'''
From https://github.com/tsc2017/Frechet-Inception-Distance
Code derived from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/gan/python/eval/python/classifier_metrics_impl.py

Usage:
    Call get_fid(images1, images2)
Args:
    images1, images2: Numpy arrays with values ranging from 0 to 255 and shape in the form [N, 3, HEIGHT, WIDTH] where N, HEIGHT and WIDTH can be arbitrary.
    dtype of the images is recommended to be np.uint8 to save CPU memory.
Returns:
    Frechet Inception Distance between the two image distributions.
'''

import tensorflow as tf
import os
import functools
import numpy as np
import time
from tensorflow.python.ops import array_ops
from mpi4py import MPI
from utils import uniform_box_sampler


tfgan = tf.contrib.gan

session = tf.compat.v1.InteractiveSession()
# A smaller BATCH_SIZE reduces GPU memory usage, but at the cost of a slight slowdown
BATCH_SIZE = 64

# Run images through Inception.
inception_images = tf.compat.v1.placeholder(tf.float32, [None, 3, None, None])
activations1 = tf.compat.v1.placeholder(tf.float32, [None, None], name='activations1')
activations2 = tf.compat.v1.placeholder(tf.float32, [None, None], name='activations2')
fcd = tfgan.eval.frechet_classifier_distance_from_activations(activations1, activations2)


def inception_activations(images=inception_images, num_splits=1):
    images = tf.transpose(images, [0, 2, 3, 1])
    size = 299
    images = tf.compat.v1.image.resize_bilinear(images, [size, size])
    generated_images_list = array_ops.split(images, num_or_size_splits=num_splits)
    activations = tf.map_fn(
        fn=functools.partial(tfgan.eval.run_inception, output_tensor='pool_3:0'),
        elems=array_ops.stack(generated_images_list),
        parallel_iterations=8,
        back_prop=False,
        swap_memory=True,
        name='RunClassifier')
    activations = array_ops.concat(array_ops.unstack(activations), 0)
    return activations


activations = inception_activations()


def get_inception_activations(inps):
    n_batches = int(np.ceil(float(inps.shape[0]) / BATCH_SIZE))
    act = np.zeros([inps.shape[0], 2048], dtype=np.float32)
    for i in range(n_batches):
        inp = inps[i * BATCH_SIZE: (i + 1) * BATCH_SIZE] / 255. * 2 - 1
        act[i * BATCH_SIZE: i * BATCH_SIZE + min(BATCH_SIZE, inp.shape[0])] = session.run(
            activations, feed_dict={inception_images: inp})
    return act


def activations2distance(act1, act2):
    return session.run(fcd, feed_dict={activations1: act1, activations2: act2})


def get_fid(images1, images2):
    assert (type(images1) == np.ndarray)
    assert (len(images1.shape) == 4)
    assert (images1.shape[1] == 3)
    print(images1.min(), images1.max(), 'minmax')
    assert (np.min(images1) >= 0 and np.max(
        images1) > 10), 'Image values should be in the range [0, 255]'
    assert (type(images2) == np.ndarray)
    assert (len(images2.shape) == 4)
    assert (images2.shape[1] == 3)
    print(images2.min(), images2.max())
    assert (np.min(images2) >= 0 and np.max(
        images2) > 10), 'Image values should be in the range [0, 255]'
    assert (images1.shape == images2.shape), 'The two numpy arrays must have the same shape'
    print('Calculating FID with %i images from each distribution' % (images1.shape[0]))
    start_time = time.time()
    act1 = get_inception_activations(images1)
    act2 = get_inception_activations(images2)
    fid = activations2distance(act1, act2)
    print('FID calculation time: %f s' % (time.time() - start_time))
    return fid


def get_fid_for_volumes(volumes1, volumes2, normalize_op=None):

    if volumes1.shape[1] == 1:
        volumes1 = np.repeat(volumes1, 3, axis=1)
        volumes2 = np.repeat(volumes2, 3, axis=1)

    if normalize_op:
        volumes1 = normalize_op(volumes1)
        volumes2 = normalize_op(volumes2)

    fids = np.mean([get_fid(volumes1[:, :, i, ...], volumes2[:, :, i, ...]) for i in range(
        volumes1.shape[2])])

    return fids


def test():
    # hvd.init()
    norm_op = lambda x: (x * 255).astype(np.int16)
    volumes1 = np.random.uniform(0, 1, size=(8, 1, 2, 8, 8))
    volumes2 = np.random.uniform(0, 1, size=(8, 1, 2, 8, 8))
    get_fid_for_volumes(volumes1, volumes2, normalize_op=norm_op)

    shape = (128, 1, 16, 64, 64)

    const_batch = np.full(shape=shape, fill_value=.05).astype(np.float32)
    rand_batch = np.random.rand(*shape)
    black_noise = const_batch + np.random.randn(*const_batch.shape) * .01

    noise_black_patches = rand_batch.copy()
    for _ in range(8):
        arr_slices = uniform_box_sampler(noise_black_patches, min_width=(32, 1, 2, 6, 6,), max_width=(32, 1, 4, 12, 12))[0]
        noise_black_patches[arr_slices] = 0

    print("black/black", get_fid_for_volumes(const_batch, const_batch, normalize_op=norm_op))
    print("rand/rand", get_fid_for_volumes(rand_batch, rand_batch, normalize_op=norm_op))
    print('black/rand', get_fid_for_volumes(const_batch, rand_batch, normalize_op=norm_op))
    print('black/black+noise', get_fid_for_volumes(const_batch, black_noise, normalize_op=norm_op))
    print('rand/black+noise', get_fid_for_volumes(rand_batch, black_noise, normalize_op=norm_op))
    print('rand/rand+blackpatches', get_fid_for_volumes(rand_batch, noise_black_patches, normalize_op=norm_op))
    print('black/rand+blackpatches', get_fid_for_volumes(const_batch, noise_black_patches, normalize_op=norm_op))




if __name__ == '__main__':
    test()