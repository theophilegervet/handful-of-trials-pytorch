class ActionRepeat(object):
    def __init__(self, env, amount):
        self._env = env
        self.amount = amount
        self.task_hor = self._env._max_episode_steps
        self.max_steps = self._env._max_episode_steps // amount

    @ property
    def observation_space(self):
        return self._env.observation_space

    @property
    def action_space(self):
        return self._env.action_space

    @property
    def dt(self):
        return self._env.dt

    def step(self, action):
        total_reward = 0

        for _ in range(self.amount):
            obs, reward, done, _ = self._env.step(action)
            total_reward += reward
            if done:
                break

        return obs, total_reward, done, {}

    def reset(self, *args, **kwargs):
        return self._env.reset(*args, **kwargs)