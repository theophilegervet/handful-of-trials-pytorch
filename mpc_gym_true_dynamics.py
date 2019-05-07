"""
CEM planning on gym environments using the ground truth dynamics.
Example usage:
python mpc_gym_true_dynamics.py MyHalfCheetah-v2 -r 4 -l 12
"""
import os
import argparse
import env # Register environments
import numpy as np
from multiprocessing import Pool
from functools import partial
import gym

from utils import Logger


class ActionRepeat(object):
    def __init__(self, env, amount):
        self._env = env
        self._amount = amount
        self._env._max_episode_steps = self._env._max_episode_steps // amount

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        total_reward = 0

        for _ in range(self._amount):
            obs, reward, _, _ = self._env.step(action)
            total_reward += reward

        return obs, total_reward, False, {}

    def reset(self, *args, **kwargs):
        return self._env.reset(*args, **kwargs)


def initializer(env):
    global global_env
    global_env = env


def evaluate(actions, state):
    global_env.reset()
    global_env.sim.set_state(state)

    score = 0
    for action in actions:
        _, reward, _, _ = global_env.step(action)
        score += reward
    return score


def cem_planner(pool, action_space, state, horizon, proposals, topk, iterations):
    action_bound = action_space.high[0]
    mean = np.zeros((horizon,) + action_space.shape)
    std = np.ones((horizon,) + action_space.shape) * action_bound

    for _ in range(iterations):
        plans = np.random.normal(mean, std, size=(proposals,) + mean.shape)
        scores = pool.map(partial(evaluate, state=state), plans)
        plans = plans[np.argsort(scores)]
        mean, std = plans[-topk:].mean(axis=0), plans[-topk:].std(axis=0)

    return mean[0]


def main(args):
    env = gym.make(args.env)
    env = ActionRepeat(env, args.repeat)

    # Pool of workers, each has its own copy of global environment variable
    pool = Pool(32, initializer, [env])

    scores = np.zeros(args.episodes)
    observations = np.zeros((args.episodes, env._max_episode_steps + 1) + env.observation_space.shape)
    actions = np.zeros((args.episodes, env._max_episode_steps) + env.action_space.shape)

    for i in range(args.episodes):
        observations[i, 0] = env.reset()

        for t in range(env._max_episode_steps):
            state = env.sim.get_state()
            actions[i, t] = cem_planner(pool, env.action_space, state, args.horizon,
                                        args.proposals, args.topk, args.iterations)
            observations[i, t + 1], reward, _, _ = env.step(actions[i, t])
            scores[i] += reward
            #print(reward)

        print(scores[i])

    print('Mean score:         ', scores.mean())
    print('Standard deviation: ', scores.std())

    param_str = '%s' % (args.repeat)
    logger = Logger(os.path.join(args.logdir, param_str))
    logger.log_scalar("scores", scores.mean(), 0)

    path = os.path.join('expert_demonstrations', args.env)
    if not os.path.exists(path):
        os.makedirs(path)
    np.save(os.path.join(path, 'expert_obs'), observations)
    np.save(os.path.join(path, 'expert_act'), actions)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('env',
                        help='OpenAI gym environment to load.')
    parser.add_argument('-r', '--repeat', type=int, default=4,
                        help='Number of times to repeat each action for.')
    parser.add_argument('-e', '--episodes', type=int, default=20,
                        help='Number of episodes to average over.')
    parser.add_argument('-l', '--horizon', type=int, default=12,
                        help='Length of each action sequence to consider.')
    parser.add_argument('-p', '--proposals', type=int, default=1000,
                        help='Number of action sequences to evaluate per iteration.')
    parser.add_argument('-k', '--topk', type=int, default=100,
                        help='Number of best action sequences to refit belief to.')
    parser.add_argument('-i', '--iterations', type=int, default=10,
                        help='Number of optimization iterations for each action sequence.')
    parser.add_argument('--logdir', type=str, default='runs/mpc_gym_true_dynamics',
                        help='Log directory for tensorboard.')
    args = parser.parse_args()
    main(args)
