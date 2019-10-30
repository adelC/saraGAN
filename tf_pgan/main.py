import argparse
import numpy as np
import os
import tensorflow as tf
import horovod.tensorflow as hvd
import time
import random
from resnet import resnet
from metrics.fid import get_fid_for_volumes
from metrics.swd_new_3d import get_swd_for_volumes

from dataset import NumpyDataset
from network import discriminator, generator
from utils import count_parameters, image_grid
from mpi4py import MPI

from tensorflow.data.experimental import AUTOTUNE


def main(args, config):
    num_phases = int(np.log2(args.final_resolution) - 1)
    var_list = None
    if args.horovod:
        print('hiiiiiii', hvd.rank())
        verbose = hvd.rank() == 0
        global_size = hvd.size()
    else:
        verbose = True
        global_size = 1

    timestamp = time.strftime("%Y-%m-%d_%H:%M", time.gmtime())
    logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'runs', timestamp)

    if verbose:
        writer = tf.summary.FileWriter(logdir=logdir)

    else:
        writer = None

    global_step = 0

    for phase in range(args.starting_phase, num_phases + 1):

        tf.reset_default_graph()
        # Get Dataset.
        size = 2 * 2 ** phase
        data_path = os.path.join(args.dataset_path, f'{size}x{size}/')
        npy_data = NumpyDataset(data_path)
        dataset = tf.data.Dataset.from_generator(npy_data.__iter__, npy_data.dtype, npy_data.shape)

        # Get DataLoader
        if args.base_batch_size:
            batch_size = max(1, args.base_batch_size // (2 ** phase))
        else:
            batch_size = max(1, 128 // size)
        # batch_size = 4

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
            update_alpha = alpha.assign(tf.maximum(alpha - alpha_update, 0))

        zdim_base = max(1, args.final_zdim // (2 ** (num_phases - 1)))
        base_shape = (1, zdim_base, 4, 4)

        noise_input_d = tf.random.normal(shape=[tf.shape(real_image_input)[0], args.latent_dim])
        gen_sample_d = generator(noise_input_d, alpha, phase, num_phases,
                                 args.base_dim, base_shape, activation=args.activation, param=args.leakiness)

        disc_fake_d = discriminator(gen_sample_d, alpha, phase, num_phases,
                                    args.base_dim, activation=args.activation, param=args.leakiness, is_reuse=False)

        disc_real_d = discriminator(real_image_input, alpha, phase, num_phases,
                                    args.base_dim, activation=args.activation, param=args.leakiness, is_reuse=True)

        wgan_disc_loss = tf.reduce_mean(disc_fake_d) - tf.reduce_mean(disc_real_d)
        gen_loss = -tf.reduce_mean(disc_fake_d)

        gamma = tf.random_uniform(shape=[tf.shape(real_image_input)[0], 1, 1, 1, 1], minval=0., maxval=1.)
        interpolates = real_image_input + gamma * (gen_sample_d - real_image_input)
        gradients = tf.gradients(discriminator(interpolates, alpha, phase,
                                               num_phases, args.base_dim, is_reuse=True, activation=args.activation,
                                               param=args.leakiness), [interpolates])[0]
        slopes = tf.sqrt(tf.reduce_sum(tf.square(gradients), reduction_indices=(1, 2, 3, 4)))
        gradient_penalty = tf.reduce_mean((slopes - args.gp_center) ** 2)

        gp_loss = args.gp_weight * gradient_penalty
        drift_loss = 1e-3 * tf.reduce_mean(disc_real_d ** 2)
        disc_loss = wgan_disc_loss + gp_loss + drift_loss

        real_ext_d = tf.reshape(resnet(real_image_input), (batch_size,))
        fake_ext_d = tf.reshape(resnet(gen_sample_d, is_reuse=True), (batch_size,))

        real_labels = tf.ones(tf.shape(real_ext_d))
        fake_labels = tf.zeros(tf.shape(real_ext_d))

        ext_d_real_loss = tf.nn.sigmoid_cross_entropy_with_logits(labels=real_labels,
                                                                  logits=real_ext_d)
        ext_d_fake_loss = tf.nn.sigmoid_cross_entropy_with_logits(labels=fake_labels,
                                                                  logits=fake_ext_d)

        ext_d_loss = tf.reduce_mean(ext_d_real_loss) + tf.reduce_mean(ext_d_fake_loss)

        ext_d_accuracy_real = tf.keras.metrics.binary_accuracy(real_labels, tf.sigmoid(real_ext_d))
        ext_d_accuracy_fake = tf.keras.metrics.binary_accuracy(fake_labels, tf.sigmoid(fake_ext_d))
        ext_d_accuracy = (ext_d_accuracy_real + ext_d_accuracy_fake) / 2

        if verbose:
            print(f"Generator parameters: {count_parameters('generator')}")
            print(f"Discriminator parameters:: {count_parameters('discriminator')}")
            print(f"Resnet parameters: {count_parameters('resnet')}")


        # Build Optimizers
        with tf.variable_scope('optim_ops'):
            optimizer_gen = tf.train.AdamOptimizer(learning_rate=0.001, beta1=0, beta2=.9)
            optimizer_disc = tf.train.AdamOptimizer(learning_rate=0.001, beta1=0, beta2=.9)
            optimizer_resnet = tf.train.AdamOptimizer()

            # Training Variables for each optimizer
            # By default in TensorFlow, all variables are updated by each optimizer, so we
            # need to precise for each one of them the specific variables to update.
            # Generator Network Variables
            gen_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='generator')
            # Discriminator Network Variables
            disc_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='discriminator')
            resnet_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='resnet')

            if args.horovod:
                optimizer_gen = hvd.DistributedOptimizer(optimizer_gen)
                optimizer_disc = hvd.DistributedOptimizer(optimizer_disc)
                optimizer_resnet = hvd.DistributedOptimizer(optimizer_resnet)

            # Create training operations
            train_gen = optimizer_gen.minimize(gen_loss, var_list=gen_vars)
            train_disc = optimizer_disc.minimize(disc_loss, var_list=disc_vars)
            train_resnet = optimizer_resnet.minimize(ext_d_loss, var_list=resnet_vars)

        with tf.name_scope('summaries'):
            # Summaries
            tf.summary.scalar('d_loss', disc_loss)
            tf.summary.scalar('g_loss', gen_loss)
            tf.summary.scalar('gp', gp_loss)
            tf.summary.scalar('ext_d_loss', ext_d_loss)

            real_image_grid = tf.transpose(real_image_input[0], (1, 2, 3, 0))
            shape = real_image_grid.get_shape().as_list()
            grid_cols = int(2 ** np.floor(np.log(shape[0]) / np.log(2)))
            grid_rows = shape[0] // grid_cols
            grid_shape = [grid_rows, grid_cols]
            real_image_grid = image_grid(real_image_grid, grid_shape, image_shape=shape[1:3],
                                         num_channels=shape[-1])

            fake_image_grid = tf.transpose(gen_sample_d[0], (1, 2, 3, 0))
            fake_image_grid = image_grid(fake_image_grid, grid_shape, image_shape=shape[1:3],
                                         num_channels=shape[-1])

            tf.summary.image('real_image', real_image_grid)
            tf.summary.image('fake_image', fake_image_grid)

            tf.summary.scalar('fake_image_min', tf.math.reduce_min(gen_sample_d))
            tf.summary.scalar('fake_image_max', tf.math.reduce_max(gen_sample_d))

            tf.summary.scalar('real_image_min', tf.math.reduce_min(real_image_input[0]))
            tf.summary.scalar('real_image_max', tf.math.reduce_max(real_image_input[0]))
            tf.summary.scalar('alpha', alpha)

            merged_summaries = tf.summary.merge_all()

        with tf.Session(config=config) as sess:

            sess.run(tf.global_variables_initializer())

            if var_list is not None:
                var_names = [v.name for v in var_list]
                trainable_variable_names = [v.name for v in tf.trainable_variables()]
                load_vars = [sess.graph.get_tensor_by_name(n) for n in var_names if n in trainable_variable_names]
                saver = tf.train.Saver(load_vars)
                print(f"Restoring session with {var_names} variables.")
                saver.restore(sess, os.path.join(logdir, f'model_{phase - 1}'))

            sess.run(init_alpha)
            if verbose:
                print(f"Begin mixing epochs in phase {phase}")
            if args.horovod:
                sess.run(hvd.broadcast_global_variables(0))

            local_step = 0
            ext_d_accuracies = []
            ext_d_losses = []

            while True:

                start = time.time()
                if local_step % 128 == 0 and args.horovod:
                    # Broadcast variables every 128 gradient steps.
                    sess.run(hvd.broadcast_global_variables(0))

                if local_step % 128 == 0 and verbose and args.profile:

                    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()

                    _, _, _, summary, d_loss, g_loss, res_acc, res_loss = sess.run(
                        [train_gen, train_disc, train_resnet, merged_summaries, disc_loss,
                         gen_loss, ext_d_accuracy, ext_d_loss],
                        options=run_options,
                        run_metadata=run_metadata)
                    writer.add_run_metadata(run_metadata, 'step%d' % global_step)

                else:
                    _, _, _, summary, d_loss, g_loss, res_acc, res_loss = sess.run(
                        [train_gen, train_disc, train_resnet, merged_summaries, disc_loss,
                         gen_loss, ext_d_accuracy, ext_d_loss],)

                end = time.time()
                img_s = global_size * batch_size / (end - start)

                if verbose:
                    writer.add_summary(summary, global_step)
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='img_s', simple_value=img_s)]),
                                       global_step)

                    ext_d_accuracies.append(res_acc)
                    ext_d_losses.append(res_loss)
                    ext_d_accuracies = ext_d_accuracies[-256:]
                    ext_d_losses = ext_d_losses[-256:]
                    mean_d_accuracy = np.mean(ext_d_accuracies)
                    mean_d_loss = np.mean(ext_d_losses)
                    writer.add_summary(tf.Summary(
                        value=[tf.Summary.Value(tag='ext_d_accuracy',
                                                simple_value=mean_d_accuracy)]),
                        global_step)

                global_step += batch_size * global_size

                sess.run(update_alpha)

                if verbose:
                    print(f"Step {global_step:09} \t"
                          f"img/s {img_s:.2f} \t "
                          f"d_loss {d_loss:.4f} \t "
                          f"g_loss {g_loss:.4f} \t "
                          f"ext_d_accuracy {mean_d_accuracy:.4f} \t "
                          f"ext_d_loss {mean_d_loss:.4f} \t "
                          f"alpha {alpha.eval():.2f}")

                local_step += 1

                if global_step >= (phase - args.starting_phase) * (args.mixing_nimg + args.stabilizing_nimg) + args.mixing_nimg:
                    break

                assert alpha.eval() >= 0

            if verbose:
                print(f"Begin stabilizing epochs in phase {phase}")

            sess.run(alpha.assign(0))
            while True:
                start = time.time()

                assert alpha.eval() == 0
                # Broadcast variables every 128 gradient steps.
                if local_step % 128 == 0 and args.horovod:
                    sess.run(hvd.broadcast_global_variables(0))

                if local_step % 128 == 0 and verbose and args.profile:
                    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
                    run_metadata = tf.RunMetadata()

                    _, _, _, summary, d_loss, g_loss, res_acc= sess.run(
                        [train_gen, train_disc, train_resnet, merged_summaries, disc_loss,
                         gen_loss, ext_d_accuracy],
                        options=run_options,
                        run_metadata=run_metadata)

                    writer.add_run_metadata(run_metadata, 'step%d' % global_step)

                else:
                    _, _, _, summary, d_loss, g_loss, res_acc = sess.run(
                        [train_gen, train_disc, train_resnet, merged_summaries, disc_loss,
                         gen_loss, ext_d_accuracy],)

                end = time.time()

                img_s = global_size * batch_size / (end - start)

                if verbose:
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='img_s', simple_value=img_s)]),
                                       global_step)
                    writer.add_summary(summary, global_step)

                    ext_d_accuracies.append(res_acc)
                    ext_d_losses.append(res_loss)
                    ext_d_accuracies = ext_d_accuracies[-256:]
                    ext_d_losses = ext_d_losses[-256:]
                    mean_d_accuracy = np.mean(ext_d_accuracies)
                    mean_d_loss = np.mean(ext_d_losses)
                    writer.add_summary(tf.Summary(
                        value=[tf.Summary.Value(tag='ext_d_accuracy',
                                                simple_value=mean_d_accuracy)]),
                        global_step)

                global_step += batch_size * global_size
                local_step += 1

                if verbose:
                    print(f"Step {global_step:09} \t"
                          f"img/s {img_s:.2f} \t "
                          f"d_loss {d_loss:.4f} \t "
                          f"g_loss {g_loss:.4f} \t "
                          f"ext_d_accuracy {res_acc:.4f} \t "
                          f"ext_d_loss {res_loss:.4f} \t "
                          f"alpha {alpha.eval():.2f}")

                if global_step >= (phase - args.starting_phase + 1) * (args.stabilizing_nimg + args.mixing_nimg):
                    break

            # Calculate Fids
            if args.calc_fids:
                num_fids_to_calculate = args.num_fids

                fids_local = []
                swds_local = []
                counter = 0
                while True:
                    if args.horovod:
                        start_loc = counter + hvd.rank() * batch_size
                    else:
                        start_loc = 0
                    real_batch = np.stack([npy_data[i] for i in range(start_loc, start_loc + batch_size)])
                    real_batch = real_batch / 1024 - 1
                    fake_batch = sess.run(gen_sample_d)

                    # [-1, 2] -> [0, 255]
                    normalize_op = lambda x: np.clip((((x / 3) + 1 / 3) * 255).astype(np.int16),
                                                     0, 255)

                    print(real_batch.min(), real_batch.max(), real_batch.shape)
                    print(fake_batch.min(), fake_batch.max(), fake_batch.shape)
                    fids_local.append(get_fid_for_volumes(real_batch, fake_batch, normalize_op))

                    if real_batch.shape[-1] >= 8:
                        swds = get_swd_for_volumes(real_batch, fake_batch)
                        swds_local.append(swds)

                    if args.horovod:
                        print(counter, batch_size, hvd.size() * batch_size)
                        counter = counter + hvd.size() * batch_size
                    else:
                        counter += batch_size

                    if counter >= num_fids_to_calculate:
                        break

                fid_local = np.mean(fids_local)
                if args.horovod:
                    fid = MPI.COMM_WORLD.allreduce(fid_local, op=MPI.SUM) / hvd.size()
                else:
                    fid = fid_local

                # swds_local explanation: list of laplacian polytope: (16x16, 32x32, 64x64..., MEAN)
                swds_local = np.array(swds_local)
                # Average over batches
                swds_local = swds_local.mean(axis=0)
                if args.horovod:
                    swds = MPI.COMM_WORLD.allreduce(swds_local, op=MPI.SUM) / hvd.size()
                else:
                    swds = swds_local

            if verbose:
                print("\n\n\n End of phase.")

                if args.calc_fids:
                    print(f"FID: {fid:.4f}")
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag='fid',
                                                                          simple_value=fid)]),
                                       global_step)


                    print(f"SWDS: {swds}")
                    for i in range(len(swds))[:-1]:
                        lod = 16 * 2 ** i
                        writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag=f'swd_{lod}',
                                                                              simple_value=swds[
                                                                                  i])]),
                                           global_step)
                    writer.add_summary(tf.Summary(value=[tf.Summary.Value(tag=f'swd_mean',
                                                                          simple_value=swds[
                                                                              -1])]),
                                           global_step)

                print(f"Ext D Accuracy {mean_d_accuracy} \n\n\n")

                # Save Session.
                # var_list = tf.trainable_variables()
                var_list = gen_vars + disc_vars
                saver = tf.train.Saver(var_list)
                saver.save(sess, os.path.join(logdir, f'model_{phase}'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset_path', type=str)
    parser.add_argument('final_resolution', type=int)
    parser.add_argument('final_zdim', type=int)
    parser.add_argument('--starting_phase', type=int, default=1)
    parser.add_argument('--base_dim', type=int, default=256)
    parser.add_argument('--latent_dim', type=int, default=256)
    parser.add_argument('--base_batch_size', type=int, default=None)
    parser.add_argument('--mixing_nimg', type=int, default=2 ** 17)
    parser.add_argument('--stabilizing_nimg', type=int, default=2 ** 17)
    parser.add_argument('--learning_rate', type=float, default=1e-3)
    parser.add_argument('--gp_center', type=float, default=1)
    parser.add_argument('--gp_weight', type=float, default=10)
    parser.add_argument('--activation', type=str, default='leaky_relu')
    parser.add_argument('--leakiness', type=float, default=0.3)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--horovod', default=False, action='store_true')
    parser.add_argument('--fp16_allreduce', default=False, action='store_true')
    parser.add_argument('--profile', default=False, action='store_true')
    parser.add_argument('--calc_fids', default=False, action='store_true')
    parser.add_argument('--num_fids', type=int, default=512)
    args = parser.parse_args()

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True

    if args.horovod:
        hvd.init()
        config.gpu_options.visible_device_list = str(hvd.local_rank())

        os.environ['KMP_AFFINITY'] = 'granularity=fine,verbose,compact,1,0'
        os.environ['KMP_BLOCKTIME'] = str(1)
        os.environ['OMP_NUM_THREADS'] = str(16)

        np.random.seed(args.seed + hvd.rank())
        tf.random.set_random_seed(args.seed + hvd.rank())
        random.seed(args.seed + hvd.rank())

        print(f"Rank {hvd.rank()} reporting!")


    else:
        np.random.seed(args.seed)
        tf.random.set_random_seed(args.seed)
        random.seed(args.seed)

    main(args, config)