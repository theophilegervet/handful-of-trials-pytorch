from torch.utils.data import TensorDataset
import time
import math

from utils import *
from model import BootstrapEnsemble
from optimizer import CEMOptimizer
from TD3 import TD3


class MPC:
    def __init__(self, args):
        """Model predictive controller.

        Arguments:
            args (DotMap): A DotMap of MPC parameters.
                .env (gym.env): Environment for which this controller will be used.
                .plan_hor (int): The planning horizon that will be used in optimization.
                .num_part (int): Number of particles used for propagation method.
                .train_epochs (int): Number of epochs to train for.
                .batches_per_epoch (int): Number of batches per training epoch.
                .obs_preproc (func): A function which modifies observations before they
                    are passed into the model.
                .pred_postproc (func): A function which takes the previous observations
                    and model predictions and returns the next observation.
                .targ_proc (func): A function which takes current observations and next
                    observations and returns the array of targetets (so that the model
                    learns the mapping obs -> targ_proc(obs, next_obs)).
                .get_reward (func): A function which computes the reward of a batch of
                    transitions.

                .model_cfg (DotMap): A DotMap of model parameters.
                    .ensemble_size (int): Number of bootstrap model.
                    .in_features (int): Size of each input sample.
                    .out_features (int): Size of each output sample.
                    .hid_features iterable(int): Size of each hidden layer, can be empty.
                    .activation: Activation function, one of 'relu', 'swish', 'tanh'.
                    .lr (float): Learning rate for optimizer.
                    .weight_decay (float): Weight decay for model parameters.
                    .dropout (float): Dropout probability.

                .opt_cfg DotMap): A DotMap of optimizer parameters.
                    .iterations (int): The number of iterations to perform during CEM
                        optimization.
                    .popsize (int): The number of candidate solutions to be sampled at
                        every iteration
                    .num_elites (int): The number of top solutions that will be used to
                        obtain the distribution at the next iteration.
        """
        self.env = args.env
        self.act_features = args.env.action_space.shape[0]
        self.obs_features = args.env.observation_space.shape[0]
        self.act_bound = args.env.action_space.high[0]

        self.plan_hor = args.plan_hor
        self.num_part = args.num_part
        self.train_epochs = args.train_epochs
        self.batches_per_epoch = args.batches_per_epoch
        self.num_nets = args.model_cfg.ensemble_size

        self.obs_preproc = args.obs_preproc
        self.pred_postproc = args.pred_postproc
        self.targ_proc = args.targ_proc
        self.get_reward = args.get_reward

        self.has_been_trained = False
        # Check arguments
        assert self.num_part % self.num_nets == 0

        # Action sequence optimizer
        self.optimizer = CEMOptimizer(args.env.action_space, self.plan_hor, **args.opt_cfg)

        # Bootstrap ensemble model
        self.model = BootstrapEnsemble(**args.model_cfg)

    def train_initial(self, obs, acts, train_split=0.8, debug_logger=None):
        """Create dataset and train bootstrap ensemble model until overfitting.

        Arguments:
            obs (list[2D np.ndarray]): observations
            acts (list[2D np.ndarray]): actions
            train_split (float): proportion of data used for training
            debug_logger: if not None, plot metrics every epoch

        Returns:
            metrics (dict): scalar metrics
            weights (dict): model weights
        """
        self.has_been_trained = True

        # Preprocess new data
        self.X, self.Y = self._preprocess_train_data(obs, acts)

        # Store input statistics for normalization
        self.model.fit_input_stats(self.X)
        
        dataset = TensorDataset(self.X, self.Y)
        train_size = int(train_split * len(dataset))
        val_size = len(dataset) - train_size

        # Bootstrap ensemble train and validation indexes
        idxs = [torch.randperm(len(dataset)) for _ in range(self.num_nets)]
        train_idxs = torch.stack([i[:train_size] for i in idxs])
        val_idxs = torch.stack([i[train_size:] for i in idxs])

        batch_size = int(len(dataset) / self.batches_per_epoch)
        train_batches = int(train_split * self.batches_per_epoch)
        val_batches = self.batches_per_epoch - train_batches

        early_stopping = EarlyStopping(patience=20)

        # Training loop
        start = time.time()
        epoch = 0
        while not early_stopping.early_stop:
            epoch += 1
            train_idxs_epoch = train_idxs[:, torch.randperm(train_size)]
            val_idxs_epoch = val_idxs[:, torch.randperm(val_size)]

            self.model.net.train()
            train_metrics = Metrics()
            for i in range(train_batches):
                X, Y = dataset[train_idxs_epoch[:, i*batch_size:(i+1)*batch_size]]
                X, Y = X.to(TORCH_DEVICE), Y.to(TORCH_DEVICE)
                train_metrics.store(self.model.update(X, Y))

            self.model.net.eval()
            val_metrics = Metrics()
            for i in range(val_batches):
                X, Y = dataset[val_idxs_epoch[:, i*batch_size:(i+1)*batch_size]]
                X, Y = X.to(TORCH_DEVICE), Y.to(TORCH_DEVICE)
                val_metrics.store(self.model.evaluate_val(X, Y))

            info_epoch = {'metrics': {}, 'weights': {}}
            info_epoch['metrics'].update(train_metrics.average())
            info_epoch['metrics'].update(val_metrics.average())
            weights = {name: param for name, param in self.model.net.named_parameters()}
            info_epoch['weights'].update(weights)

            if debug_logger is not None and epoch % 10 == 0:
                for k, v in info_epoch['metrics'].items():
                    debug_logger.log_scalar(k, v, epoch)
                for k, v in info_epoch['weights'].items():
                    debug_logger.log_histogram(k, v, epoch)

            # Stop if mean validation cross-entropy across all models stops decreasing
            early_stopping.step(info_epoch['metrics']['xentropy/val_mean'], self.model.net, info_epoch)

        # Load policy with best validation loss
        info_best = early_stopping.load_best(self.model.net)
        info_best['metrics']['time/train_time'] = time.time() - start
        info_best['metrics']['time/train_epochs'] = epoch - early_stopping.patience

        return info_best['metrics'], info_best['weights']

    def train_iteration(self, obs, acts):
        """Add new data to training set and train bootstrap ensemble model for train_epochs
        epochs over the whole training set. To be called after train_initial() has been called
        the first time.

        Arguments:
            obs (list[2D np.ndarray]): new observations
            acts (list[2D np.ndarray]): new actions

        Returns:
            metrics (dict): scalar metrics
            weights (dict): model weights
        """
        # Preprocess new data and add to training set
        X_new, Y_new = self._preprocess_train_data(obs, acts)
        self.X, self.Y = torch.cat((self.X, X_new)), torch.cat((self.Y, Y_new))

        # Record mse and cross-entropy on new test data
        metrics = {}
        self.model.net.eval()
        metrics.update(self.model.evaluate_test(X_new.to(TORCH_DEVICE), Y_new.to(TORCH_DEVICE)))

        # Store input statistics for normalization
        self.model.fit_input_stats(self.X)

        dataset = TensorDataset(self.X, self.Y)
        batch_size = int(len(dataset) / self.batches_per_epoch)

        # Training loop
        start = time.time()
        for epoch in range(self.train_epochs):
            idxs = torch.stack([torch.randperm(len(dataset)) for _ in range(self.num_nets)])

            self.model.net.train()
            epoch_metrics = Metrics()
            for i in range(self.batches_per_epoch):
                X, Y = dataset[idxs[:, i * batch_size:(i + 1) * batch_size]]
                X, Y = X.to(TORCH_DEVICE), Y.to(TORCH_DEVICE)
                epoch_metrics.store(self.model.update(X, Y))

        metrics.update(epoch_metrics.average())
        metrics['time/train_time'] = time.time() - start
        metrics['time/train_epochs'] = self.train_epochs

        weights = {name: param for name, param in self.model.net.named_parameters()}

        return metrics, weights

    def _preprocess_train_data(self, obs, acts):
        """Preprocess observations and actions for dynamics model training set.

        Arguments:
            obs (list[2D np.ndarray]): observations
            acts (list[2D np.ndarray]): actions

        Returns:
            X (2D torch.Tensor): inputs
            Y (2D torch.Tensor): targets
        """
        X = [np.concatenate([self.obs_preproc(o[:-1]), a], axis=1) for o, a in zip(obs, acts)]
        Y = [self.targ_proc(o[:-1], o[1:]) for o in obs]
        X, Y = np.concatenate(X, axis=0), np.concatenate(Y, axis=0)
        X, Y = torch.from_numpy(X).float(), torch.from_numpy(Y).float()
        return X, Y

    def act(self, obs):
        """Return the action that this controller would take for a single observation obs.

        Arguments:
            obs (1D numpy.ndarray): Observation.

        Returns: Action (1D numpy.ndarray).
        """
        if not self.has_been_trained:
            return np.random.uniform(-self.act_bound, self.act_bound, (self.act_features,)), {}

        particle_info = Metrics()
        obs = torch.from_numpy(obs[np.newaxis]).float()
        act = self.act_parallel(obs, particle_info=particle_info)[0].cpu().numpy()

        return act, particle_info.average()

    def act_parallel(self, obs, particle_info=None):
        """Return the action that this controller would take for each of the observations
        in obs. Used to sample multiple rollouts in parallel.

        Arguments:
            obs (2D torch.Tensor or np.ndarray): Observations (num_obs, obs_features) on CPU.
            particle_info (utils.Metrics): Dictionary to keep track of particle statistics
                or None.

        Returns: Actions (2D torch.Tensor) on CPU.
        """
        if isinstance(obs, np.ndarray):
            obs = torch.from_numpy(obs).float()

        self.model.net.eval()
        plans = self.optimizer.obtain_solution(obs, self._compile_score, particle_info)

        # Return the first action of each plan
        return plans[:, 0]

    @torch.no_grad()
    def _compile_score(self, plans, cur_obs, particle_info, batch_size=1000000):
        """Compute score of plans (sequences of actions) starting at observations in
        cur_obs under the learned dynamics.

        Arguments:
            plans (3D torch.Tensor): Sequences of actions of shape
                (num_obs, num_plans, plan_hor * act_features).
            cur_obs (2D torch.Tensor): Starting observations to compile scores of shape
                (num_obs, obs_features).
            particle_info (utils.Metrics): Dictionary to keep track of particle statistics
                or None.
            batch_size (int): Batch size for parallel computation.

        Returns:
            scores (2D torch.Tensor): Score of plans of shape (num_obs, num_plans).
        """
        assert batch_size % self.num_part == 0
        num_obs, num_plans = plans.shape[:2]

        # Reshape plans for parallel computation
        # 1 - (num_obs, num_plans, plan_hor * act_features)
        # 2 - (num_obs, num_plans, plan_hor, act_features)
        # 3 - (num_obs, num_plans, num_part, plan_hor, act_features)
        # 4 - (num_obs * num_plans * num_part, plan_hor, act_features)
        plans = plans.view(num_obs, num_plans, self.plan_hor, self.act_features)
        plans = plans.unsqueeze(2).expand(-1, -1, self.num_part, -1, -1).contiguous()
        plans = plans.view(-1, self.plan_hor, self.act_features)

        # Reshape observations for parallel computation
        # 1 - (num_obs, obs_features)
        # 2 - (num_obs * num_plans * num_part, obs_features)
        obs = cur_obs.repeat(num_plans * self.num_part, 1)

        dataset = TensorDataset(obs, plans)
        num_batches = math.ceil(len(dataset) / batch_size)
        scores = torch.zeros(num_obs * num_plans * self.num_part).to(TORCH_DEVICE)

        # Compute scores in parallel
        # Across starting observations, plans per observation and particles per plan
        for i in range(num_batches):
            obs, plans = dataset[i * batch_size:(i+1) * batch_size]
            obs, plans = obs.to(TORCH_DEVICE), plans.to(TORCH_DEVICE)
            alives = torch.ones(obs.shape[0]).to(TORCH_DEVICE)

            for t in range(self.plan_hor):
                acts = plans[:, t]
                next_obs = self._predict_next_obs_divide(obs, acts)

                # Measure diversity among particles by observation std dev
                if particle_info is not None:
                    obs_std = next_obs.view(-1, self.num_part, self.obs_features).std(dim=1).cpu()
                    metrics = {}; log_statistics(metrics, obs_std, 'particle/obs_std')
                    particle_info.store(metrics)

                # Compute rewards and done flags
                rewards, dones = self.get_reward(obs, acts, next_obs)
                alives = torch.min(alives, 1 - dones)
                scores[i * batch_size:(i+1) * batch_size] += alives * rewards

                obs = next_obs
                if alives.sum() == 0:
                    break

        scores = scores.view(num_obs, num_plans, self.num_part)

        # Measure diversity among particles by score std dev
        if particle_info is not None:
            metrics = {}
            log_statistics(metrics, scores.mean(dim=-1).cpu(), 'particle/score_mean')
            log_statistics(metrics, scores.std(dim=-1).cpu(), 'particle/score_std')
            particle_info.store(metrics)

        # Average score over particles
        return scores.mean(dim=-1).cpu()

    def _predict_next_obs_average(self, obs, acts):
        """Predict next observation by averaging predictions of all models in ensemble.

        Arguments:
            obs (2D torch.Tensor): Observations.
            acts (2D torch.Tensor): Actions.

        Returns: Next observations (2D torch.Tensor).
        """
        # Preprocess observations
        proc_obs = self.obs_preproc(obs)

        # Predict next observations by averaging ensemble predictions
        input = torch.cat((proc_obs, acts), dim=-1).repeat(self.num_nets, 1, 1)
        preds = self.model.sample(input)
        avg_preds = preds.mean(dim=0)

        # Postprocess predictions
        return self.pred_postproc(obs, avg_preds)

    def _predict_next_obs_divide(self, obs, acts):
        """Predict next observation by dividing predictions among models in ensemble.

         Arguments:
            obs (2D torch.Tensor): Observations.
            acts (2D torch.Tensor): Actions.

        Returns: Next observations (2D torch.Tensor).
        """
        # Preprocess observations
        proc_obs = self.obs_preproc(obs)

        # Predict next observations, by dividing particles among models in ensemble
        input = self._to_bootstrap_shape(torch.cat((proc_obs, acts), dim=-1))
        preds = self.model.sample(input)
        preds = self._from_bootstrap_shape(preds)

        # Postprocess predictions
        return self.pred_postproc(obs, preds)

    def _to_bootstrap_shape(self, input):
        # Reshape matrix to be processed by bootstrap ensemble model
        # 1 - (x * num_part, num_features)
        # 2 - (x, num_nets, num_parts / num_nets, num_features)
        # 3 - (num_nets, x * (num_parts / num_nets), num_features)
        num_features = input.shape[-1]
        reshaped = input.view(-1, self.num_nets, self.num_part // self.num_nets, num_features)
        reshaped = reshaped.transpose(0, 1).contiguous().view(self.num_nets, -1, num_features)
        return reshaped

    def _from_bootstrap_shape(self, input):
        # Reshape 3D tensor processed by bootstrap ensemble model to matrix
        # 1 - (num_nets, x * (num_parts / num_nets), num_features)
        # 2 - (num_nets, x, num_part / num_nets, num_features)
        # 3 - (x * num_part, num_features)
        num_features = input.shape[-1]
        reshaped = input.view(self.num_nets, -1, self.num_part // self.num_nets, num_features)
        reshaped = reshaped.transpose(0, 1).contiguous().view(-1, num_features)
        return reshaped