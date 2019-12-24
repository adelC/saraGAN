import argparse
import numpy as np
import os
import tensorflow as tf
import horovod.tensorflow as hvd
import time
import random
from metrics import (get_fid_for_volumes, inception_activations, get_swd_for_volumes,
                     get_normalized_root_mse, get_mean_squared_error, get_psnr, get_ssim)
from dataset import NumpyDataset
from utils import count_parameters, image_grid, parse_tuple
from mpi4py import MPI
import nvgpu
import psutil
import subprocess
import importlib

from tensorflow.data.experimental import AUTOTUNE


def main(args, config):

    if args.horovod:
        verbose = hvd.rank() == 0
        global_size = hvd.size()
    else:
        verbose = True
        global_size = 1

    timestamp = time.strftime("%Y-%m-%d_%H:%M:%S", time.gmtime())
    logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runs', args.architecture, timestamp)

    if verbose:
        writer = tf.summary.FileWriter(logdir=logdir)
        print("Arguments passed:")
        print(args)
        print(f"Saving files to {logdir}")

    else:
        writer = None

    final_shape = parse_tuple(args.final_shape)
    final_resolution = final_shape[-1]
    num_phases = int(np.log2(final_resolution) - 1)

    var_list = None
    global_step = 0

    for phase in range(1, num_phases + 1):

        tf.reset_default_graph()
        # Get Dataset.
        size = 2 * 2 ** phase
        data_path = os.path.join(args.dataset_path, f'{size}x{size}/')
        npy_data = NumpyDataset(data_path, '/scratch', copy_files=hvd.local_rank() == 0)
        dataset = tf.data.Dataset.from_generator(npy_data.__iter__, npy_data.dtype, npy_data.shape)

        # Get DataLoader
        batch_size = max(1, args.max_batch_size // ((2 ** (phase - 1)) * global_size))
        assert batch_size * global_size <= 128

        if verbose:
            print(f"Using local batch size of {batch_size} and global batch size of {batch_size * global_size}")

        if args.horovod:
            dataset.shard(hvd.size(), hvd.rank())

        # Lay out the graph.
        real_image_input = dataset. \
            shuffle(len(npy_data)). \
            batch(batch_size, drop_remainder=True). \
            map(lambda x: tf.cast(x, tf.float32) / 1024 - 1, num_parallel_calls=AUTOTUNE). \
            prefetch(AUTOTUNE). \
            repeat(). \
            make_one_shot_iterator(). \
            get_next()

        real_image_input = real_image_input + tf.random.normal(tf.shape(real_image_input)) * .01

        with tf.variable_scope('alpha'):
            alpha = tf.Variable(1, name='alpha', dtype=tf.float32)
            # Alpha init
            init_alpha = alpha.assign(1)

            # Specify alpha update op for mixing phase.
            num_steps = args.mixing_nimg // (batch_size * global_size)
            alpha_update = 1 / num_steps
            # noinspection PyTypeChecker
            update_alpha = alpha.assign(tf.maximum(alpha - alpha_update, 0))

        zdim_base = max(1, final_shape[1] // (2 ** (num_phases - 1)))
        base_shape = (1, zdim_base, 4, 4)

        z = tf.random.normal(shape=[tf.shape(real_image_input)[0], args.latent_dim])
        gen_sample = generator(z, alpha, phase, num_phases,
                               args.base_dim, base_shape, activation=args.activation,
                               # is_training=True,
                               param=args.leakiness)

        # Discriminator Training
        disc_fake_d = discriminator(tf.stop_gradient(gen_sample), alpha, phase, num_phases,
                                    args.base_dim, args.latent_dim, activation=args.activation, param=args.leakiness)
        disc_real = discriminator(real_image_input, alpha, phase, num_phases,
                                  args.base_dim, args.latent_dim, activation=args.activation, param=args.leakiness, is_reuse=True)

        wgan_disc_loss = disc_fake_d - disc_real

        gamma = tf.random_uniform(shape=[tf.shape(real_image_input)[0], 1, 1, 1, 1], minval=0., maxval=1.)
        interpolates = gamma * real_image_input + (1 - gamma) * tf.stop_gradient(gen_sample)
        gradients = tf.gradients(discriminator(interpolates, alpha, phase,
                                               num_phases, args.base_dim, args.latent_dim,
                                               is_reuse=True, activation=args.activation,
                                               param=args.leakiness), [interpolates])[0]
        slopes = tf.sqrt(tf.reduce_sum(tf.square(gradients), reduction_indices=(1, 2, 3, 4)))
        gradient_penalty = (slopes - args.gp_center) ** 2
        gp_loss = (args.base_gp_weight * 2 ** phase) * gradient_penalty

        drift_loss = 1e-3 * disc_real ** 2
        disc_loss = tf.reduce_mean(wgan_disc_loss + gp_loss + drift_loss)

        # Generator training.
        disc_fake_g = discriminator(gen_sample, alpha, phase, num_phases, args.base_dim, args.latent_dim,
                                    activation=args.activation, param=args.leakiness, is_reuse=True)

        gen_loss = -tf.reduce_mean(disc_fake_g)

        if verbose:
            print(f"Generator parameters: {count_parameters('generator')}")
            print(f"Discriminator parameters:: {count_parameters('discriminator')}")
        gen_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='generator')
        disc_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='discriminator')

        # Build Optimizers
        with tf.variable_scope('optim_ops'):
            g_lr = args.learning_rate
            d_lr = args.learning_rate

            if args.horovod:
                if args.g_scaling == 'sqrt':
                    g_lr = g_lr * np.sqrt(hvd.size())
                elif args.g_scaling == 'linear':
                    g_lr = g_lr * hvd.size()
                elif args.g_scaling == 'none':
                    pass
                else:
                    raise ValueError(args.g_scaling)

                if args.d_scaling == 'sqrt':
                    d_lr = d_lr * np.sqrt(hvd.size())
                elif args.d_scaling == 'linear':
                    d_lr = d_lr * hvd.size()
                elif args.d_scaling == 'none':
                    pass
                else:
                    raise ValueError(args.d_scaling)

            optimizer_gen = tf.train.AdamOptimizer(learning_rate=g_lr, beta1=args.beta1, beta2=args.beta2)
            optimizer_disc = tf.train.AdamOptimizer(learning_rate=d_lr, beta1=args.beta1, beta2=args.beta2)

            if args.horovod:
                optimizer_gen = hvd.DistributedOptimizer(optimizer_gen)
                optimizer_disc = hvd.DistributedOptimizer(optimizer_disc)

            g_gradients = optimizer_gen.compute_gradients(gen_loss, var_list=gen_vars)
            d_gradients = optimizer_disc.compute_gradients(disc_loss, var_list=disc_vars)

            g_norms = tf.stack([tf.norm(grad) for grad, var in g_gradients if grad is not None])
            max_g_norm = tf.reduce_max(g_norms)
            d_norms = tf.stack([tf.norm(grad) for grad, var in d_gradients if grad is not None])
            max_d_norm = tf.reduce_max(d_norms)

            # 128 is very large. Might want to clip lower, keep track of max norms in Tensorboard.
            g_clipped_grads = [(tf.clip_by_norm(grad, clip_norm=128), var) for grad, var in g_gradients]
            d_clipped_grads = [(tf.clip_by_norm(grad, clip_norm=128), var) for grad, var in d_gradients]

            train_gen = optimizer_gen.apply_gradients(g_clipped_grads)
            train_disc = optimizer_disc.apply_gradients(d_clipped_grads)

            # # Create training operations
            # train_gen = optimizer_gen.minimize(gen_loss, var_list=gen_vars)
            # train_disc = optimizer_disc.minimize(disc_loss, var_list=disc_vars)

            ema = tf.train.ExponentialMovingAverage(decay=args.ema_beta)
            ema_op = ema.apply(gen_vars)
            # Transfer EMA values to original variables
            ema_update_weights = tf.group(
                [tf.assign(var, ema.average(var)) for var in gen_vars])

            if args.calc_metrics:
                inception_images = tf.compat.v1.placeholder(tf.float32, [None, 3, None, None])
                activations = inception_activations(inception_images)

        with tf.name_scope('summaries'):
            # Summaries
            tf.summary.scalar('d_loss', disc_loss)
            tf.summary.scalar('g_loss', gen_loss)
            tf.summary.scalar('gp', tf.reduce_mean(gp_loss))

            tf.summary.scalar('max_g_grad_norm', max_g_norm)
            tf.summary.scalar('max_d_grad_norm', max_d_norm)

            real_image_grid = tf.transpose(real_image_input[0], (1, 2, 3, 0))
            shape = real_image_grid.get_shape().as_list()
            grid_cols = int(2 ** np.floor(np.log(np.sqrt(shape[0])) / np.log(2)))
            grid_rows = shape[0] // grid_cols
            grid_shape = [grid_rows, grid_cols]
            real_image_grid = image_grid(real_image_grid, grid_shape, image_shape=shape[1:3],
                                         num_channels=shape[-1])

            fake_image_grid = tf.transpose(gen_sample[0], (1, 2, 3, 0))
            fake_image_grid = image_grid(fake_image_grid, grid_shape, image_shape=shape[1:3],
                                         num_channels=shape[-1])

            tf.summary.image('real_image', real_image_grid)
            tf.summary.image('fake_image', fake_image_grid)

            tf.summary.scalar('fake_image_min', tf.math.reduce_min(gen_sample))
            tf.summary.scalar('fake_image_max', tf.math.reduce_max(gen_sample))

            tf.summary.scalar('real_image_min', tf.math.reduce_min(real_image_input[0]))
            tf.summary.scalar('real_image_max', tf.math.reduce_max(real_image_input[0]))
            tf.summary.scalar('alpha', alpha)

            tf.summary.scalar('g_lr', g_lr)
            tf.summary.scalar('d_lr', d_lr)

            merged_summaries = tf.summary.merge_all()

        with tf.Session(config=config) as sess:

            sess.run(tf.global_variables_initializer())

            if var_list is not None and phase > args.starting_phase:
                var_names = [v.name for v in var_list]
                trainable_variable_names = [v.name for v in tf.trainable_variables()]
                load_vars = [sess.graph.get_tensor_by_name(n) for n in var_names if n in trainable_variable_names]
                saver = tf.train.Saver(load_vars)
                if verbose:
                    print(f"Restoring session with {var_names} variables.")
                saver.restore(sess, os.path.join(logdir, f'model_{phase - 1}'))

            elif var_list is not None and args.continue_path and phase == args.starting_phase:
                var_names = [v.name for v in var_list]
                trainable_variable_names = [v.name for v in tf.trainable_variables()]
                load_vars = [sess.graph.get_tensor_by_name(n) for n in var_names if n in trainable_variable_names]
                saver = tf.train.Saver(load_vars)
                if verbose:
                    print(f"Restoring session with {var_names} variables.")
                saver.restore(sess, os.path.join(args.continue_path))

            var_list = gen_vars + disc_vars

            if phase < args.starting_phase:
                continue

            if phase == args.starting_phase:
                sess.run(alpha.assign(args.starting_alpha))
            else:
                sess.run(init_alpha)

            if verbose:
                print(f"Begin mixing epochs in phase {phase}")
            if args.horovod:
                sess.run(hvd.broadcast_global_variables(0))

            local_step = 0
            while True:
                start = time.time()
                if local_step % 2048 == 0 and local_step > 1:
                    sess.run(ema_update_weights)
                    if args.horovod:
                        # Broadcast variables every 1024 gradient steps.
                        sess.run(hvd.broadcast_global_variables(0))
                    saver = tf.train.Saver(var_list)
                    if verbose:
                        saver.save(sess, os.path.join(logdir, f'model_{phase}_ckpt_{global_step}'))

                _, _, summary, d_loss, g_loss = sess.run(
                     [train_gen, train_disc, merged_summaries,
                      disc_loss, gen_loss])
                global_step += batch_size * global_size
                local_step += 1

                end = time.time()
                img_s = global_size * batch_size / (end - start)
                if verbose:
                    writer.add_summary(summary, global_step)
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='img_s', simple_value=img_s)]),
                                       global_step)

                    print(f"Step {global_step:09} \t"
                          f"img/s {img_s:.2f} \t "
                          f"d_loss {d_loss:.4f} \t "
                          f"g_loss {g_loss:.4f} \t "
                          f"alpha {alpha.eval():.2f}")

                if global_step >= (phase - args.starting_phase) * (args.mixing_nimg + args.stabilizing_nimg) \
                        + args.mixing_nimg:
                    break

                sess.run(update_alpha)
                sess.run(ema_op)

                assert alpha.eval() >= 0

            if verbose:
                print(f"Begin stabilizing epochs in phase {phase}")

            sess.run(alpha.assign(0))

            while True:
                start = time.time()
                assert alpha.eval() == 0
                # Broadcast variables every 1024 gradient steps.
                if local_step % 1024 == 0 and local_step > 0:

                    sess.run(ema_update_weights)
                    if args.horovod:
                        sess.run(hvd.broadcast_global_variables(0))
                    saver = tf.train.Saver(var_list)
                    if verbose:
                        saver.save(sess, os.path.join(logdir, f'model_{phase}_ckpt_{global_step}'))

                _, _, summary, d_loss, g_loss = sess.run(
                     [train_gen, train_disc, merged_summaries,
                      disc_loss, gen_loss])

                global_step += batch_size * global_size
                local_step += 1

                end = time.time()
                img_s = global_size * batch_size / (end - start)
                if verbose:
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='img_s', simple_value=img_s)]),
                                       global_step)
                    writer.add_summary(summary, global_step)

                    print(f"Step {global_step:09} \t"
                          f"img/s {img_s:.2f} \t "
                          f"d_loss {d_loss:.4f} \t "
                          f"g_loss {g_loss:.4f} \t "
                          f"alpha {alpha.eval():.2f}")

                sess.run(ema_op)

                if global_step >= (phase - args.starting_phase + 1) * (args.stabilizing_nimg + args.mixing_nimg):
                    # if verbose:
                    #     run_metadata = tf.RunMetadata()
                    #     opts = tf.profiler.ProfileOptionBuilder.float_operation()
                    #     g = tf.get_default_graph()
                    #     flops = tf.profiler.profile(g, run_meta=run_metadata, cmd='op', options=opts)
                    #     writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='graph_flops',
                    #                                                           simple_value=flops.total_float_ops)]),
                    #                        global_step)
                    #
                    #     # Print memory info.
                    #     try:
                    #         print(nvgpu.gpu_info())
                    #     except subprocess.CalledProcessError:
                    #         pid = os.getpid()
                    #         py = psutil.Process(pid)
                    #         print(f"CPU Percent: {py.cpu_percent()}")
                    #         print(f"Memory info: {py.memory_info()}")

                    break

            # Calculate metrics.
            calc_swds: bool = size >= 16
            print(npy_data.shape)
            calc_ssims: bool = min(npy_data.shape[1:]) >= 32

            if args.calc_metrics:
                fids_local = []
                swds_local = []
                psnrs_local = []
                mses_local = []
                nrmses_local = []
                ssims_local = []

                counter = 0
                while True:
                    if args.horovod:
                        start_loc = counter + hvd.rank() * batch_size
                    else:
                        start_loc = 0
                    real_batch = np.stack([npy_data[i] for i in range(start_loc, start_loc + batch_size)])
                    real_batch = real_batch.astype(np.int16) - 1024
                    fake_batch = sess.run(gen_sample).astype(np.float32)

                    # Turn fake batch into HUs and clip to training range.
                    fake_batch = (np.clip(fake_batch, -1, 2) * 1024).astype(np.int16)

                    if verbose:
                        print('real min, max', real_batch.min(), real_batch.max())
                        print('fake min, max', fake_batch.min(), fake_batch.max())
                    
                    fid_normalization = lambda x: (256 * (((x / 1024) + 1) / 3)).astype(np.int16)

                    fids_local.append(get_fid_for_volumes(sess, activations, inception_images, real_batch, fake_batch, normalize_op=fid_normalization))
                    if calc_swds:
                        swds = get_swd_for_volumes(real_batch, fake_batch)
                        swds_local.append(swds)

                    psnr = get_psnr(real_batch, fake_batch)
                    if calc_ssims:
                        ssim = get_ssim(real_batch, fake_batch)
                        ssims_local.append(ssim)
                    mse = get_mean_squared_error(real_batch, fake_batch)
                    nrmse = get_normalized_root_mse(real_batch, fake_batch)

                    psnrs_local.append(psnr)
                    mses_local.append(mse)
                    nrmses_local.append(nrmse)

                    if args.horovod:
                        counter = counter + global_size * batch_size
                    else:
                        counter += batch_size

                    if counter >= args.num_metric_samples:
                        break

                fid_local = np.mean(fids_local)
                psnr_local = np.mean(psnrs_local)
                ssim_local = np.mean(ssims_local)
                mse_local = np.mean(mses_local)
                nrmse_local = np.mean(nrmses_local)

                if args.horovod:
                    fid = MPI.COMM_WORLD.allreduce(fid_local, op=MPI.SUM) / hvd.size()
                    psnr = MPI.COMM_WORLD.allreduce(psnr_local, op=MPI.SUM) / hvd.size()
                    ssim = MPI.COMM_WORLD.allreduce(ssim_local, op=MPI.SUM) / hvd.size()
                    mse = MPI.COMM_WORLD.allreduce(mse_local, op=MPI.SUM) / hvd.size()
                    nrmse = MPI.COMM_WORLD.allreduce(nrmse_local, op=MPI.SUM) / hvd.size()
                else:
                    fid = fid_local
                    psnr = psnr_local
                    ssim = ssim_local
                    mse = mse_local
                    nrmse = nrmse_local

                if calc_swds:
                    swds_local = np.array(swds_local)
                    # Average over batches
                    swds_local = swds_local.mean(axis=0)
                    if args.horovod:
                        swds = MPI.COMM_WORLD.allreduce(swds_local, op=MPI.SUM) / hvd.size()
                    else:
                        swds = swds_local

                if calc_ssims:
                    ssims_local = np.array(ssims_local)
                    # Average over batches
                    ssims_local = ssims_local.mean(axis=0)
                    if args.horovod:
                        ssims = MPI.COMM_WORLD.allreduce(ssims_local, op=MPI.SUM) / hvd.size()
                    else:
                        ssims = ssims_local


                if verbose:
                    print(f"FID: {fid:.4f}")
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='fid',
                                                                          simple_value=fid)]),
                                       global_step)

                    print(f"PSNR: {psnr:.4f}")
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='psnr',
                                                                          simple_value=psnr)]),
                                       global_step)

                    print(f"SSIM: {fid:.4f}")
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='ssim',
                                                                          simple_value=ssim)]),
                                       global_step)

                    print(f"MSE: {mse:.4f}")
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='mse',
                                                                          simple_value=mse)]),
                                       global_step)

                    print(f"Normalized Root MSE: {nrmse:.4f}")
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='nrmse',
                                                                          simple_value=nrmse)]),
                                       global_step)

                    if calc_swds:
                        print(f"SWDS: {swds}")
                        for i in range(len(swds))[:-1]:
                            lod = 16 * 2 ** i
                            writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag=f'swd_{lod}',
                                                                                  simple_value=swds[
                                                                                      i])]),
                                               global_step)
                        writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag=f'swd_mean',
                                                                              simple_value=swds[
                                                                                  -1])]), global_step)
                    if calc_ssims:
                        print(f"SWDS: {ssims}")
                        for i in range(len(ssims))[:-1]:
                            lod = 16 * 2 ** i
                            writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag=f'swd_{lod}',
                                                                                  simple_value=ssims[
                                                                                      i])]),
                                               global_step)
                        writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag=f'swd_mean',
                                                                              simple_value=ssims[
                                                                                  -1])]), global_step)

            if verbose:
                print("\n\n\n End of phase.")

                # Save Session.
                sess.run(ema_update_weights)
                saver = tf.train.Saver(var_list)
                saver.save(sess, os.path.join(logdir, f'model_{phase}'))

            if args.ending_phase:
                if phase == args.ending_phase:
                    print("Reached final phase, breaking.")
                    break


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('architecture', type=str)
    parser.add_argument('dataset_path', type=str)
    parser.add_argument('final_shape', type=str, help="'(c, z, y, x)', e.g. '(1, 64, 128, 128)'")
    parser.add_argument('--starting_phase', type=int, default=None, required=True)
    parser.add_argument('--ending_phase', type=int, default=None, required=True)
    parser.add_argument('--base_dim', type=int, default=None, required=True)
    parser.add_argument('--latent_dim', type=int, default=None, required=True)
    parser.add_argument('--max_batch_size', type=int, default=128)
    parser.add_argument('--mixing_nimg', type=int, default=2 ** 17)
    parser.add_argument('--stabilizing_nimg', type=int, default=2 ** 17)
    parser.add_argument('--learning_rate', type=float, default=5e-4)
    parser.add_argument('--gp_center', type=float, default=0)
    parser.add_argument('--base_gp_weight', type=float, default=1)
    parser.add_argument('--activation', type=str, default='leaky_relu')
    parser.add_argument('--leakiness', type=float, default=0.2)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--horovod', default=False, action='store_true')
    parser.add_argument('--calc_metrics', default=False, action='store_true')
    parser.add_argument('--g_annealing', default=1,
                        type=float, help='generator annealing rate, 1 -> no annealing.')
    parser.add_argument('--d_annealing', default=1,
                        type=float, help='discriminator annealing rate, 1 -> no annealing.')
    parser.add_argument('--num_metric_samples', type=int, default=512)
    parser.add_argument('--beta1', type=float, default=0)
    parser.add_argument('--beta2', type=float, default=0.99)
    parser.add_argument('--ema_beta', type=float, default=0.99)
    parser.add_argument('--d_scaling', default='sqrt', choices=['linear', 'sqrt', 'none'],
                        help='How to scale discriminator learning rate with horovod size.')
    parser.add_argument('--g_scaling', default='sqrt', choices=['linear', 'sqrt', 'none'],
                        help='How to scale generator learning rate with horovod size.')
    parser.add_argument('--continue_path', default=None, type=str)
    parser.add_argument('--starting_alpha', default=1, type=float)
    args = parser.parse_args()

    gopts = tf.GraphOptions(place_pruned_graph=True)
    config = tf.ConfigProto(graph_options=gopts)
    config.gpu_options.allow_growth = True

    discriminator = importlib.import_module(f'networks.{args.architecture}.discriminator').discriminator
    generator = importlib.import_module(f'networks.{args.architecture}.generator').generator

    if args.horovod:
        hvd.init()
        config.gpu_options.visible_device_list = str(hvd.local_rank())

        np.random.seed(args.seed + hvd.rank())
        tf.random.set_random_seed(args.seed + hvd.rank())
        random.seed(args.seed + hvd.rank())

        print(f"Rank {hvd.rank()}:{hvd.local_rank()} reporting!")

    else:
        np.random.seed(args.seed)
        tf.random.set_random_seed(args.seed)
        random.seed(args.seed)

    main(args, config)
