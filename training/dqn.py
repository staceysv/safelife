import numpy as np

import torch
import torch.optim as optim

from safelife.helper_utils import load_kwargs

from .base_algo import BaseAlgo
from .utils import round_up


USE_CUDA = torch.cuda.is_available()


class ReplayBuffer(BaseAlgo):
    def __init__(self, capacity):
        self.capacity = capacity
        self.idx = 0
        self.buffer = np.zeros(capacity, dtype=object)

    def push(self, *data):
        self.buffer[self.idx % self.capacity] = data
        self.idx += 1

    def sample(self, batch_size):
        sub_buffer = self.buffer[:self.idx]
        data = np.random.choice(sub_buffer, batch_size, replace=False)
        return zip(*data)

    def __len__(self):
        return min(self.idx, self.capacity)


class DQN(object):
    data_logger = None

    num_steps = 0

    gamma = 0.97
    training_batch_size = 64
    optimize_interval = 16
    learning_rate = 3e-4
    epsilon = 0.0  # for exploration

    replay_initial = 40000
    replay_size = 100000
    target_update_interval = 10000

    report_interval = 256
    test_interval = 100000

    compute_device = torch.device('cuda' if USE_CUDA else 'cpu')

    training_envs = None
    testing_envs = None

    checkpoint_attribs = ('training_model', 'target_model', 'optimizer')

    def __init__(self, training_model, target_model, **kwargs):
        load_kwargs(self, kwargs)
        assert self.training_envs is not None

        self.training_model = training_model.to(self.compute_device)
        self.target_model = target_model.to(self.compute_device)
        self.optimizer = optim.Adam(
            self.training_model.parameters(), lr=self.learning_rate)
        self.replay_buffer = ReplayBuffer(self.replay_size)

        self.load_checkpoint()

    def update_target(self):
        self.target_model.load_state_dict(self.training_model.state_dict())

    def run_test_envs(self):
        # Just run one episode of each test environment.
        # Assumes that the environments themselves handle logging.
        for env in self.testing_envs:
            state = env.reset()
            done = False
            while not done:
                state = torch.tensor([state], device=self.compute_device, dtype=torch.float32)
                qvals = self.training_model(state).detach().cpu().numpy().ravel()
                state, reward, done, info = env.step(np.argmax(qvals))

    def collect_data(self):
        states = [
            e.last_state if hasattr(e, 'last_state') else e.reset()
            for e in self.training_envs
        ]
        tensor_states = torch.tensor(states, device=self.compute_device, dtype=torch.float32)
        qvals = self.training_model(tensor_states).detach().cpu().numpy()

        num_states, num_actions = qvals.shape
        actions = np.argmax(qvals, axis=-1)
        random_actions = np.random.randint(num_actions, size=num_states)
        use_random = np.random.random(num_states) < self.epsilon
        actions = np.choose(use_random, [actions, random_actions])

        for env, state, action in zip(self.training_envs, states, actions):
            next_state, reward, done, info = env.step(action)
            if done:
                next_state = env.reset()
            env.last_state = next_state
            self.replay_buffer.push(state, action, reward, next_state, done)

        self.num_steps += len(states)

    def optimize(self, report=False):
        if len(self.replay_buffer) < self.replay_initial:
            return

        state, action, reward, next_state, done = \
            self.replay_buffer.sample(self.training_batch_size)

        state = torch.tensor(state, device=self.compute_device, dtype=torch.float32)
        next_state = torch.tensor(next_state, device=self.compute_device, dtype=torch.float32)
        action = torch.tensor(action, device=self.compute_device, dtype=torch.int64)
        reward = torch.tensor(reward, device=self.compute_device, dtype=torch.float32)
        done = torch.tensor(done, device=self.compute_device, dtype=torch.float32)

        q_values = self.training_model(state)
        next_q_values = self.target_model(next_state).detach()

        q_value = q_values.gather(1, action.unsqueeze(1)).squeeze(1)
        next_q_value, next_action = next_q_values.max(1)
        expected_q_value = reward + self.gamma * next_q_value * (1 - done)

        loss = torch.mean((q_value - expected_q_value)**2)

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        if report and self.data_logger is not None:
            self.data_logger.log_scalars({
                "loss": loss,
                "epsilon": self.epsilon,
                "qvals/model_mean": q_values.mean().item(),
                "qvals/model_max": q_values.max(1)[0].mean().item(),
                "qvals/target_mean": next_q_values.mean().item(),
                "qvals/target_max": next_q_value.mean().item(),
            }, self.num_steps, 'dqn')

    def train(self, steps):
        needs_report = True

        for _ in range(int(steps / len(self.training_envs))):
            num_steps = self.num_steps
            next_opt = round_up(num_steps, self.optimize_interval)
            next_update = round_up(num_steps, self.target_update_interval)
            next_report = round_up(num_steps, self.report_interval)
            next_test = round_up(num_steps, self.test_interval)

            self.collect_data()

            num_steps = self.num_steps

            if len(self.replay_buffer) < self.replay_initial:
                continue

            if num_steps >= next_report:
                needs_report = True

            if num_steps >= next_opt:
                self.optimize(needs_report)
                needs_report = False

            if num_steps >= next_update:
                self.target_model.load_state_dict(self.training_model.state_dict())

            self.save_checkpoint()

            if num_steps >= next_test:
                self.run_test_envs()
