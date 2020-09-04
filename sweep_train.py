#!/usr/bin/env python3

"""
Main entry point for starting a training job.
"""

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
import cProfile
import wandb
from training.logging_setup import setup_logging, setup_data_logger


def parse_args():

    parser = argparse.ArgumentParser(description="""
        Run agent training using proximal policy optimization.

        This will set up the data/log directories, optionally install any needed
        dependencies, start tensorboard, configure loggers, and start the actual
        training loop. If the data directory already exists, it will prompt for
        whether the existing data should be overwritten or appended. The latter
        allows for training to be restarted if interrupted.
        """)
    parser.add_argument('--data_dir', default="tmp_safe_life", type=str,
        help="the directory in which to store this run's data")
    parser.add_argument('--shutdown', action="store_true",
        help="Shut down the system when the job is complete"
        "(helpful for running remotely).")
    parser.add_argument('--port', default=6006, type=int,
        help="Port on which to run tensorboard.")
    parser.add_argument('--run-type', choices=('train', 'benchmark', 'inspect'),
        default='train',
        help="What to do once the algorithm and environments have been loaded. "
        "If 'train', train the model. If 'benchmark', run the model on testing "
        "environments. If 'inspect', load an ipython prompt for interactive "
        "debugging.")
    parser.add_argument('-p', '--impact-penalty', type=float)
    parser.add_argument('--penalty-baseline',
        choices=('starting-state', 'inaction'), default='starting-state')
    parser.add_argument('-e', '--env-type', default='prune-still')
    parser.add_argument('--curriculum', default="progress_estimate", type=str,
        help='Curriculum type ("uniform" or "progress_estimate")')
    parser.add_argument('--algo', choices=('ppo', 'dqn'), default='ppo')
    parser.add_argument('--seed', default=None, type=int)
    parser.add_argument('-s', '--steps', type=float, default=6e6,
        help='Length of training in steps (default: 6e6).')
    parser.add_argument('-w', '--wandb', action='store_false',
        help='Use wandb for analytics.')
    parser.add_argument('--project', default=None,
        help='[Entity and] project for wandb. Eg: "safelife/multiagent" or "multiagent"')
    parser.add_argument('--ensure-gpu', action='store_true',
        help="Check that the machine we're running on has CUDA support")
    parser.add_argument('--profile', action='store_true',
        help="Profile the main thread")
    args = parser.parse_args()



# Start tensorboard


    return args


# Start training!

def main(args):
    # Setup the directories
    data_dir = os.path.realpath(args.data_dir)

    safety_dir = os.path.realpath(os.path.join(__file__, '../'))
# Build the safelife C extensions.
# By making the build lib the same as the base folder, the extension
# should just get built into the source directory.
    subprocess.run([
        "python3", os.path.join(safety_dir, "setup.py"),
        "build_ext", "--build-lib", safety_dir
    ])



    job_name = os.path.basename(data_dir)
    sys.path.insert(1, safety_dir)  # ensure current directory is on the path
    os.chdir(safety_dir)


    if os.path.exists(data_dir) and args.data_dir is not None and args.run_type == 'train':
        print("The directory '%s' already exists. "
              "Would you like to overwrite the old data, append to it, or abort?" %
              data_dir)
        response = 'overwrite' if job_name.startswith('tmp') else None
        while response not in ('overwrite', 'append', 'abort'):
            response = input("(overwrite / append / abort) > ")
        if response == 'overwrite':
            print("Overwriting old data.")
            shutil.rmtree(data_dir)
        elif response == 'abort':
            print("Aborting.")
            exit()

    os.makedirs(data_dir, exist_ok=True)

    logger = setup_logging(data_dir, debug=(args.run_type == 'inspect'))

    if args.port and not args.wandb:
        tb_proc = subprocess.Popen([
            "tensorboard", "--logdir_spec", job_name + ':' + data_dir, '--port', str(args.port)])
    else:
        tb_proc = None


    try:
        import numpy as np
        import torch
        from training.env_factory import build_environments
        from safelife.random import set_rng


        main_seed = np.random.SeedSequence(args.seed)
        logger.info("COMMAND ARGUMENTS: %s", ' '.join(sys.argv))
        logger.info("SETTING GLOBAL SEED: %i", main_seed.entropy)
        set_rng(np.random.default_rng(main_seed))
        torch.manual_seed(main_seed.entropy & (2**31 - 1))
        if args.seed is not None:
            # Note that this may slow down performance
            # See https://pytorch.org/docs/stable/notes/randomness.html#cudnn
            torch.backends.cudnn.deterministic = True

        logger.info("TRAINING RUN: %s", job_name)
        logger.info("ON HOST: %s", platform.node())
        if args.ensure_gpu:
            assert torch.cuda.is_available(), "CUDA support requested but not available!"

        data_logger = setup_data_logger(data_dir, args.run_type, args.wandb)

        if args.wandb:
            wandb.init(name=job_name, config=args,
                       entity="stacey", project="safelife_sweep")
            args = wandb.config
        else:
            print("Not using wandb")

        training_envs, testing_envs = build_environments(args, main_seed, data_logger)

        if args.algo == 'ppo':
            from training.models import SafeLifePolicyNetwork
            from training.ppo import PPO

            obs_shape = training_envs[0].observation_space.shape
            model = SafeLifePolicyNetwork(obs_shape)
            algo = PPO(
                model,
                training_envs=training_envs,
                testing_envs=testing_envs,
                data_logger=data_logger)

        elif args.algo == 'dqn':
            from training.models import SafeLifeQNetwork
            from training.dqn import DQN

            obs_shape = training_envs[0].observation_space.shape
            train_model = SafeLifeQNetwork(obs_shape)
            target_model = SafeLifeQNetwork(obs_shape)
            algo = DQN(
                train_model, target_model,
                training_envs=training_envs,
                testing_envs=testing_envs,
                data_logger=data_logger)
        else:
            logging.error("Unexpected algorithm type '%s'", args.algo)
            raise ValueError("unexpected algorithm type")

        if args.run_type == "train":
            algo.train(int(args.steps))
        elif args.run_type == "benchmark":
            algo.run_episodes(testing_envs, num_episodes=1000)
        elif args.run_type == "inspect":
            from IPython import embed
            print('')
            embed()


    except Exception:
        logging.exception("Ran into an unexpected error. Aborting training.")
    finally:
        if tb_proc is not None:
            tb_proc.kill()
        #if args.wandb:
            #wandb.join()
        if args.shutdown:
            # Shutdown in 3 minutes.
            # Enough time to recover if it crashed at the start.
            subprocess.run("sudo shutdown +3", shell=True)
            logging.critical("Shutdown commenced, but keeping ssh available...")
            subprocess.run("sudo rm -f /run/nologin", shell=True)

if __name__ == "__main__":
    args = parse_args()
    if args.profile:
        cProfile.run("main(args, logger)")
    else:
        print(args)
        main(args)
