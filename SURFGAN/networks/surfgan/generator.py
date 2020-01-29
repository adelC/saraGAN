import os
from networks.ops import *
import time


def num_filters(phase, num_phases, base_dim):
    filter_list = [1024, 1024, 256, 256, 256, 256, 128, 64]
    assert num_phases == len(filter_list)
    filters = filter_list[phase - 1]
    return filters


def generator_in(x, filters, shape, activation, param=None):
    with tf.variable_scope('dense'):
        x = dense(x, np.product(shape) * filters, activation, param=param)
        x = apply_bias(x)
        x = act(x, activation, param=param)
    x = tf.reshape(x, [-1, filters] + list(shape))
    return x


def generator_block(x, filters_out, activation, param=None):
    with tf.variable_scope('upsample'):
        x = upscale3d(x)

    with tf.variable_scope('conv_1'):
        shape = x.get_shape().as_list()[2:]
        kernel = [k(s) for s in shape]
        x = conv3d(x, filters_out, kernel, activation, param=param)
        x = apply_bias(x)
        x = act(x, activation, param=param)
        # x = pixel_norm(x)

    return x


def generator(x, alpha, phase, num_phases, base_dim, base_shape, activation, param=None):
    with tf.variable_scope('generator'):
        with tf.variable_scope('generator_in'):
            x = generator_in(x, filters=base_dim, shape=base_shape[1:], activation=activation, param=param)

        x_upsample = None

        for i in range(2, phase + 1):

            if i == phase:
                with tf.variable_scope(f'to_rgb_{phase - 1}'):
                    x_upsample = upscale3d(to_rgb(x, channels=base_shape[0]))

            filters_out = num_filters(i, num_phases, base_dim)
            with tf.variable_scope(f'generator_block_{i}'):
                x = generator_block(x, filters_out, activation=activation, param=param)

        with tf.variable_scope(f'to_rgb_{phase}'):
            x_out = to_rgb(x, channels=base_shape[0])

        if x_upsample is not None:
            x_out = alpha * x_upsample + (1 - alpha) * x_out

        return x_out


if __name__ == '__main__':

    os.environ['OMP_NUM_THREADS'] = str(16)

    num_phases = 8
    base_dim = 1024
    latent_dim = 1024
    base_shape = [1, 1, 4, 4]
    for phase in range(8, 9):
        shape = [1, latent_dim]
        x = tf.random.normal(shape=shape)
        y = generator(x, 0.5, phase, num_phases, base_dim, base_shape, activation='leaky_relu',
                      param=0.3)

        loss = tf.reduce_sum(y)
        optim = tf.train.GradientDescentOptimizer(1e-5)
        train = optim.minimize(loss)
        print('Generator output shape:', y.shape)

        for p in tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope='generator'):
            print(np.product(p.shape), p.name)  # i.name if you want just a name

        print('Total generator variables:',
              sum(np.product(p.shape) for p in tf.trainable_variables('generator')))

        # with tf.Session() as sess:
        #     sess.run(tf.global_variables_initializer())
        #     start = time.time()
        #     sess.run(train)

        #     end = time.time()

        #     print(f"{end - start} seconds")