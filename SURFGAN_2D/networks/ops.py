import tensorflow as tf
import numpy as np


def k(x):
    if x < 3:
        return 1
    else:
        return 3


def calculate_gain(activation, param=None):
    linear_fns = ['linear', 'conv1d', 'conv2d', 'conv3d', 'conv_transpose1d', 'conv_transpose2d', 'conv_transpose3d']
    if activation in linear_fns or activation == 'sigmoid':
        return 1
    elif activation == 'tanh':
        return 5.0 / 3
    elif activation == 'relu':
        return np.sqrt(2.0)
    elif activation == 'leaky_relu':
        assert param is not None
        if not isinstance(param, bool) and isinstance(param, int) or isinstance(param, float):
            # True/False are instances of int, hence check above
            negative_slope = param
        else:
            raise ValueError("negative_slope {} not a valid number".format(param))
        return np.sqrt(2.0 / (1 + negative_slope ** 2))
    else:
        raise ValueError("Unsupported nonlinearity {}".format(activation))


def get_weight(shape, activation, lrmul=1, param=None):
    fan_in = np.prod(shape[:-1])
    gain = calculate_gain(activation, param)
    he_std = gain / np.sqrt(fan_in)
    init_std = 1.0 / lrmul
    runtime_coef = he_std * lrmul
    return tf.get_variable('weight', shape=shape,
                           initializer=tf.initializers.random_normal(0, init_std)) * runtime_coef


def apply_bias(x, lrmul=1):
    b = tf.get_variable('bias', shape=[x.shape[1]], initializer=tf.initializers.zeros()) * lrmul
    b = tf.cast(b, x.dtype)
    if len(x.shape) == 2:
        return x + b
    else:
        return x + tf.reshape(b, [1, -1, 1, 1])


def dense(x, fmaps, activation, lrmul=1, param=None):
    if len(x.shape) > 2:
        x = tf.reshape(x, [-1, np.prod([d.value for d in x.shape[1:]])])
    w = get_weight([x.shape[1].value, fmaps], activation, lrmul=lrmul, param=param)
    w = tf.cast(w, x.dtype)
    return tf.matmul(x, w)


def conv2d(x, fmaps, kernel, activation, param=None, lrmul=1):
    w = get_weight([*kernel, x.shape[1].value, fmaps], activation, param=param, lrmul=lrmul)
    w = tf.cast(w, x.dtype)
    return tf.nn.conv2d(x, w, strides=[1, 1, 1, 1], padding='SAME', data_format='NCHW')


def leaky_relu(x, alpha_lr=0.2):
    with tf.variable_scope('leaky_relu'):
        alpha_lr = tf.constant(alpha_lr, dtype=x.dtype, name='alpha_lr')

        @tf.custom_gradient
        def func(x):
            y = tf.maximum(x, x * alpha_lr)

            @tf.custom_gradient
            def grad(dy):
                dx = tf.where(y >= 0, dy, dy * alpha_lr)
                return dx, lambda ddx: tf.where(y >= 0, ddx, ddx * alpha_lr)

            return y, grad

        return func(x)


def act(x, activation, param=None):
    if activation == 'leaky_relu':
        assert param is not None
        return leaky_relu(x, alpha_lr=param)
    elif activation == 'linear':
        return x
    else:
        raise ValueError(f"Unknown activation {activation}")


# def num_filters(phase, num_phases, base_dim):
#     num_downscales = int(np.log2(base_dim / 32))
#     filters = min(base_dim // (2 ** (phase - num_phases + num_downscales)), base_dim)
#     return filters


def num_filters(phase, num_phases, base_dim=None, size=None):
    if size == 'small':
        filter_list = [256, 256, 256, 128, 64, 32, 16, 8]
    elif size == 'medium':
        filter_list = [512, 512, 512, 256, 128, 64, 32]
    elif size == 'big':
        filter_list = [1024, 1024, 1024, 512, 256, 128, 64]
    else:
        raise ValueError(f"Unknown size: {size}")
    assert num_phases == len(filter_list)
    filters = filter_list[phase - 1]
    return filters


def to_rgb(x, channels=3):
    return apply_bias(conv2d(x, channels, (1, 1), activation='linear'))


def from_rgb(x, filters_out, activation, param=None):
    x = conv2d(x, filters_out, (1, 1), activation, param)
    x = apply_bias(x)
    x = act(x, activation, param=param)
    return x


def avg_unpool2d(x, factor=2, gain=1):
    if gain != 1:
        x = x * gain

    if factor == 1:
        return x

    x = tf.transpose(x, [2, 3, 1, 0])  # [B, C, H, W] -> [H, W, C, B]
    x = tf.expand_dims(x, 0)
    x = tf.tile(x, [factor ** 2, 1, 1, 1, 1])
    x = tf.batch_to_space_nd(x, [factor, factor], [[0, 0], [0, 0]])
    x = tf.transpose(x[0], [3, 2, 0, 1])  # [H, W, C, B] -> [B, C, H, W]
    return x


def avg_pool2d(x, factor=2, gain=1):
    if gain != 1:
        x *= gain

    if factor == 1:
        return x

    ksize = [1, 1, factor, factor]
    return tf.nn.avg_pool2d(x, ksize=ksize, strides=ksize, padding='VALID', data_format='NCHW')


def upscale2d(x, factor=2):
    with tf.variable_scope('upscale_2d'):
        @tf.custom_gradient
        def func(x):
            y = avg_unpool2d(x, factor)

            @tf.custom_gradient
            def grad(dy):
                dx = avg_pool2d(dy, factor, gain=factor ** 2)
                return dx, lambda ddx: avg_unpool2d(ddx, factor)

            return y, grad

        return func(x)


def downscale2d(x, factor=2):
    with tf.variable_scope('downscale_2d'):
        @tf.custom_gradient
        def func(x):
            y = avg_pool2d(x, factor)

            @tf.custom_gradient
            def grad(dy):
                dx = avg_unpool2d(dy, factor, gain=1 / factor ** 2)
                return dx, lambda ddx: avg_pool2d(ddx, factor)

            return y, grad

        return func(x)


def pixel_norm(x, epsilon=1e-8):
    with tf.variable_scope('pixel_norm'):
        return x * tf.rsqrt(tf.reduce_mean(tf.square(x), axis=1, keepdims=True) + epsilon)


def minibatch_stddev_layer(x, group_size=4):
    with tf.variable_scope('minibatch_std'):
        group_size = tf.minimum(group_size, tf.shape(x)[0])
        s = x.shape
        y = tf.reshape(x, [group_size, -1, s[1], s[2], s[3], s[4]])
        y = tf.cast(y, tf.float32)
        y -= tf.reduce_mean(y, axis=0, keepdims=True)
        y = tf.reduce_mean(tf.square(y), axis=0)
        y = tf.sqrt(y + 1e-8)
        y = tf.reduce_mean(y, axis=[1, 2, 3, 4], keepdims=True)
        y = tf.cast(y, x.dtype)
        y = tf.tile(y, [group_size, 1, s[2], s[3], s[4]])
        return tf.concat([x, y], axis=1)


def instance_norm(x, epsilon=1e-8):
    assert len(x.shape) == 4  # NCHW
    with tf.variable_scope('instance_norm'):
        x -= tf.reduce_mean(x, axis=[2, 3], keepdims=True)
        x *= tf.rsqrt(tf.reduce_mean(tf.square(x), axis=[2, 3, 4], keepdims=True) + epsilon)
        return x


def apply_noise(x):
    assert len(x.shape) == 4  # NCHW
    with tf.variable_scope('apply_noise'):
        noise = tf.random_normal([tf.shape(x)[0], 1, x.shape[2], x.shape[3]])
        noise_strength = tf.get_variable('noise_strength', shape=[], initializer=tf.initializers.zeros())
        return x + noise * noise_strength


def style_mod(x, dlatent, activation, param=None):
    with tf.variable_scope('style_mod'):
        style = apply_bias(dense(dlatent, fmaps=x.shape[1] * 2, activation=activation, param=param))
        style = tf.reshape(style, [-1, 2, x.shape[1]] + [1] * (len(x.shape) - 2))
        return x * (style[:, 0] + 1) + style[:, 1]

