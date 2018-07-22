import argparse
import math
import os
import random
import sys
import time

from tabulate import tabulate
from PIL import Image
from pathlib import Path

import chainer
import chainermn
import chainer.functions as cf
import cupy
import numpy as np
from chainer.backends import cuda

sys.path.append(".")
sys.path.append("..")
import glow

from hyperparams import Hyperparameters
from model import InferenceModel, GenerativeModel
from optimizer import Optimizer


def to_gpu(array):
    if isinstance(array, np.ndarray):
        return cuda.to_gpu(array)
    return array


def to_cpu(array):
    if isinstance(array, cupy.ndarray):
        return cuda.to_cpu(array)
    return array


def printr(string):
    sys.stdout.write(string)
    sys.stdout.write("\r")


# return z of same shape as x
def merge_factorized_z(factorized_z, factor=2):
    z = None
    for zi in reversed(factorized_z):
        xp = cuda.get_array_module(zi.data)
        z = zi.data if z is None else xp.concatenate((zi.data, z), axis=1)
        z = glow.nn.chainer.functions.unsqueeze(z, factor, xp)
    return z


def preprocess(image, num_bits_x):
    num_bins_x = 2**num_bits_x
    if num_bits_x < 8:
        image = np.floor(image / (2**(8 - num_bits_x)))
    image = image / num_bins_x - 0.5
    if image.ndim == 3:
        image = image.transpose((2, 0, 1))
    elif image.ndim == 4:
        image = image.transpose((0, 3, 1, 2))
    else:
        raise NotImplementedError
    return image


def main():
    try:
        os.mkdir(args.snapshot_path)
    except:
        pass

    comm = chainermn.create_communicator()
    device = comm.intra_rank
    print("device", device, "/", comm.size)
    cuda.get_device(device).use()
    xp = cupy

    num_bins_x = 2**args.num_bits_x
    images = None
    
    if comm.rank == 0:
        assert args.dataset_format in ["png", "npy"]
        files = Path(args.dataset_path).glob("*.{}".format(
            args.dataset_format))
        if args.dataset_format == "png":
            images = []
            for filepath in files:
                image = np.array(Image.open(filepath)).astype("float32")
                image = preprocess(image, args.num_bits_x)
                images.append(image)
            assert len(images) > 0
            images = np.asanyarray(images)
        elif args.dataset_format == "npy":
            images = []
            for filepath in files:
                array = np.load(filepath).astype("float32")
                array = preprocess(array, args.num_bits_x)
                images.append(array)
            assert len(images) > 0
            num_files = len(images)
            images = np.asanyarray(images)
            images = images.reshape((num_files * images.shape[1], ) +
                                    images.shape[2:])
        else:
            raise NotImplementedError

        assert args.image_size == images.shape[2]

        x_mean = np.mean(images)
        x_var = np.var(images)

        print(
            tabulate([
                ["#", len(images)],
                ["mean", x_mean],
                ["var", x_var],
            ]))

    dataset = chainermn.scatter_dataset(images, comm, shuffle=True)

    hyperparams = Hyperparameters(args.snapshot_path
                                  if comm.rank == 0 else None)
    hyperparams.levels = args.levels
    hyperparams.depth_per_level = args.depth_per_level
    hyperparams.nn_hidden_channels = args.nn_hidden_channels
    hyperparams.image_size = (args.image_size, args.image_size)
    hyperparams.num_bits_x = args.num_bits_x
    hyperparams.lu_decomposition = args.lu_decomposition
    if comm.rank == 0:
        hyperparams.serialize(args.snapshot_path)

    if comm.rank == 0:
        print(
            tabulate([
                ["levels", hyperparams.levels],
                ["depth_per_level", hyperparams.depth_per_level],
                ["nn_hidden_channels", hyperparams.nn_hidden_channels],
                ["image_size", hyperparams.image_size],
                ["lu_decomposition", hyperparams.lu_decomposition],
                ["num_bits_x", hyperparams.num_bits_x],
            ]))

    encoder = InferenceModel(hyperparams, hdf5_path=args.snapshot_path)
    encoder.to_gpu()

    optimizer = chainermn.create_multi_node_optimizer(
        chainer.optimizers.Adam(alpha=1e-4), comm)
    optimizer.setup(encoder.params)

    current_training_step = 0
    num_pixels = hyperparams.image_size[0] * hyperparams.image_size[1]

    # Training loop
    for iteration in range(args.training_steps):
        sum_loss = 0
        total_batch = 0
        start_time = time.time()
        iterator = chainer.iterators.SerialIterator(
            dataset, args.batch_size, repeat=False)

        # Data dependent initialization
        if encoder.need_initialize:
            for data in iterator:
                x = to_gpu(np.asanyarray(data))
                encoder.initialize_actnorm_weights(x)
                break

        for batch_index, data in enumerate(iterator):
            x = to_gpu(np.asanyarray(data))
            batch_size = x.shape[0]

            x += xp.random.uniform(0, 1.0 / num_bins_x, size=x.shape)
            factorized_z, logdet = encoder(x, reduce_memory=args.reduce_memory)
            logdet -= math.log(num_bins_x) * num_pixels
            negative_log_likelihood = 0
            for zi in factorized_z:
                negative_log_likelihood += glow.nn.chainer.functions.standard_normal_nll(
                    zi)
            denom = math.log(2.0) * batch_size * num_pixels
            loss = (negative_log_likelihood - logdet) / denom
            encoder.cleargrads()
            loss.backward()
            optimizer.update()

            current_training_step += 1
            total_batch += 1

            sum_loss += float(loss.data)
            if comm.rank == 0:
                printr(
                    "Iteration {}: Batch {} / {} - loss: {:.8f} - nll: {:.8f} - log_det: {:.8f}".
                    format(iteration + 1, batch_index + 1,
                           len(dataset) // batch_size, float(loss.data),
                           float(negative_log_likelihood.data) / denom,
                           float(logdet.data) / denom))

            if batch_index % 100 == 0:
                encoder.serialize(args.snapshot_path)

        # Check model reversibility
        rev_x_mean = None
        rev_x_var = None
        z_mean = None
        z_var = None
        if True:
            with chainer.no_backprop_mode():
                decoder = encoder.reverse()
                factorized_z, logdet = encoder(x)
                rev_x, _ = decoder(factorized_z)
                rev_x_mean = float(xp.mean(rev_x.data))
                rev_x_var = float(xp.var(rev_x.data))

                z = merge_factorized_z(factorized_z)
                z_mean = float(xp.mean(z))
                z_var = float(xp.var(z))

        if comm.rank == 0:
            elapsed_time = time.time() - start_time
            print(
                "\033[2KIteration {} - loss: {:.5f} - z: mean={:.5f} var={:.5f} - rev_x: mean={:.5f} var={:.5f} - elapsed_time: {:.3f} min".
                format(iteration + 1, sum_loss / total_batch, z_mean, z_var,
                       rev_x_mean, rev_x_var, elapsed_time / 60))
            encoder.serialize(args.snapshot_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-path", "-dataset", type=str, required=True)
    parser.add_argument("--dataset-format", "-ext", type=str, required=True)
    parser.add_argument(
        "--snapshot-path", "-snapshot", type=str, default="snapshot")
    parser.add_argument("--batch-size", "-b", type=int, default=32)
    parser.add_argument("--reduce-memory", action="store_true")
    parser.add_argument("--training-steps", "-i", type=int, default=100000)
    parser.add_argument("--depth-per-level", "-depth", type=int, default=32)
    parser.add_argument("--levels", "-levels", type=int, default=5)
    parser.add_argument("--nn-hidden-channels", "-nn", type=int, default=512)
    parser.add_argument("--num-bits-x", "-bits", type=int, default=8)
    parser.add_argument("--lu-decomposition", "-lu", action="store_true")
    parser.add_argument("--image-size", type=int, required=True)
    args = parser.parse_args()
    main()
