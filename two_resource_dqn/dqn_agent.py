import random

import torch
import torch.nn as nn

from collections import deque
from enum import Enum
from typing import NamedTuple


class QNet(nn.Module):
    def __init__(self, n_images, vector_dim, n_action):
        super(QNet, self).__init__()
        self.conv1 = nn.Conv2d(in_channels=3 * n_images, out_channels=8, kernel_size=3, stride=1)
        self.bn1 = nn.BatchNorm2d(8)
        self.conv2 = nn.Conv2d(in_channels=8, out_channels=16, kernel_size=3, stride=1)
        self.bn2 = nn.BatchNorm2d(16)

        self.fc_vec = nn.Linear(in_features=vector_dim, out_features=50)

        self.fc1 = nn.Linear(in_features=12594, out_features=400)
        self.fc2 = nn.Linear(in_features=400, out_features=n_action)

        self.act_conv = nn.Softplus()
        self.act_fc = nn.Softplus()

    def forward(self, image_tensor: torch.Tensor, vector_tensor: torch.Tensor) -> torch.Tensor:
        x_im = self.act_conv(self.bn1(self.conv1(image_tensor)))
        x_im = self.act_conv(self.bn2(self.conv2(x_im)))
        x_im = x_im.flatten()
        x_int = self.act_fc(self.fc_vec(vector_tensor))
        x = torch.cat([x_im, x_int], dim=0)
        x = self.act_fc(self.fc1(x))
        output = self.fc2(x)
        return output


class BufferType(Enum):
    observation = 0
    action = 1


class Observation(NamedTuple):
    image: torch.Tensor
    vector: torch.Tensor


class Experience(NamedTuple):
    observation: Observation
    action: int
    next_observation: Observation


class ReplayBuffer:
    def __init__(self, buffer_size):
        self.buffer_size = buffer_size
        self.buffer_experience = deque(maxlen=buffer_size)
        self.n_experience = len(self.buffer_experience)

    def append(self, observation: Observation, action, next_observation: Observation):
        self.buffer_experience.append(Experience(
            observation=observation,
            action=action,
            next_observation=next_observation
        ))
        self.n_experience = len(self.buffer_experience)

    def get_single_experience(self, time_step):
        """
        Return a single experience instance
        :param time_step:
        :return:
        """
        assert self.n_experience - 1 > time_step, "Sample time step must be less than number of experience minus one."
        return self.buffer_experience[time_step]

    def get_batch_experience(self, batch_size):
        """
        Return batch list of experience instance
        :param batch_size:
        :return:
        """
        batch = []
        for i in range(batch_size):
            index = random.choice(range(self.n_experience - 1))
            batch.append(self.get_single_experience(index))
        return batch

    def clear(self):
        self.buffer_experience.clear()
        self.n_experience = len(self.buffer_experience)


class DQNAgent:
    """ Classical Deep Q Network Agent
    """

    def __init__(self, config, n_action, action_size, shape_vector_obs, shape_obs_image, eps_start, device):
        # Network
        self.input_time_horizon = int(config["qn"]["input_time_horizon"])
        self.action_size = action_size
        self.shape_obs_image = shape_obs_image  # Like (64, 64, 3)
        self.shape_vector_obs = shape_vector_obs  # Like  (2,)
        self.n_action = n_action
        self._device = device
        self.qnet = QNet(n_images=self.input_time_horizon,
                         vector_dim=2,
                         n_action=n_action).to(device)
        self.qnet_support = QNet(n_images=1,
                                 vector_dim=2,
                                 n_action=n_action).to(device)
        self.qnet_support.load_state_dict(self.qnet.state_dict())

        # Optimization
        self.training_start_from = int(config["qn"]["training_start_from"])
        self.learning_rate = float(config["dnn"]["learning_rate"])
        self.adam_eps = float(config["dnn"]["adam_eps"])
        self.batch_size = int(config["dnn"]["batch_size"])
        self.iteration = int(config["qn"]["iteration"])
        self._optimizer = torch.optim.Adam(
            params=self.qnet.parameters(),
            lr=self.learning_rate,
            eps=self.adam_eps,
            weight_decay=float(config["dnn"]["weight_decay"])
        )

        # Replay Buffer
        self.replay_buffer_size = int(config["qn"]["replay_buffer_size"])
        self.replay_buffer = ReplayBuffer(buffer_size=self.replay_buffer_size)

        # Other initialization
        self._reward_discount = float(config["qn"]["reward_discount"])
        self.__eps_e_greedy = eps_start
        self.time_tick = 0
        self._prev_observation = None
        self._prev_action = None

    def reset_for_new_episode(self):
        self._prev_observation = None
        self._prev_action = None

    def step(self, observation, done) -> torch.Tensor:
        with torch.no_grad():
            im_tensor, vec_tensor = self.obs_to_tensor(observation)

            # Stock into the replay buffer
            obs_now = Observation(image=im_tensor, vector=vec_tensor)
            if None not in (self._prev_action, self._prev_observation):
                self.replay_buffer.append(
                    observation=self._prev_observation,
                    action=self._prev_action,
                    next_observation=obs_now
                )

            # Get Action
            greedy_action, q_vec = self.get_greedy_action(im_tensor.to(self._device), vec_tensor.to(self._device))
            if random.random() < self.eps_e_greedy:
                next_action = random.choice(range(self.n_action))
            else:
                next_action = greedy_action

        # Train Agent
        if self.replay_buffer.n_experience > self.training_start_from:
            self.train()
        else:
            if self.replay_buffer.n_experience % 500 == 0:
                print(f"Buffer : {self.replay_buffer.n_experience}/{self.replay_buffer_size}")

        # Copy Q net to the support network
        if self.time_tick == self.iteration:
            self.time_tick = 0
            self.qnet_support.load_state_dict(self.qnet.state_dict())
            print("Q-net Iteration Done.")
        else:
            self.time_tick += 1

        # Some visualization
        # if self._prev_observation is not None:
        #     print(f"reward : {self.reward(self._prev_observation.vector, vec_tensor)}, Q-val : {q_vec[next_action]}")

        # Set for next step
        self._prev_observation = obs_now
        self._prev_action = next_action

        return next_action

    def train(self):
        self._optimizer.zero_grad()
        data_batch = self.replay_buffer.get_batch_experience(batch_size=self.batch_size)
        loss = torch.zeros(1).to(self._device)
        for experience in data_batch:
            action = experience.action
            im_tensor = experience.observation.image.to(self._device)
            vec_tensor = experience.observation.vector.to(self._device)
            q_val = self.qnet(im_tensor, vec_tensor)[action]

            next_im_tensor = experience.next_observation.image.to(self._device)
            next_vec_tensor = experience.next_observation.vector.to(self._device)
            q_vec_next = self.qnet_support(next_im_tensor, next_vec_tensor).max().detach()
            # Assuming shaping reward with \Phi(s) = log P(s)
            reward_tensor = self.reward(vec_tensor, next_vec_tensor).to(self._device)
            # print(reward_tensor.cpu().numpy())

            target = reward_tensor + self._reward_discount * q_vec_next
            loss += (target - q_val).clamp(min=-1, max=1).pow(2)
        loss /= self.batch_size
        loss.backward()
        self._optimizer.step()

    def reward(self, vector_obs: torch.Tensor, next_vector_obs: torch.Tensor):
        # Shaping reward-enhanced reward
        # reward = - 0.1 * next_vector_obs.pow(2.0).sum()
        # reward -= - 0.1 * vector_obs.pow(2.0).sum()
        # reward *= self._reward_discount/(1.0 - self._reward_discount)

        reward = - 0.01 * vector_obs.pow(2.0).sum()
        # Clip reward
        reward = reward.clamp(min=-3, max=+3)
        return reward.detach()

    @property
    def eps_e_greedy(self):
        return self.__eps_e_greedy

    @eps_e_greedy.setter
    def eps_e_greedy(self, v):
        self.__eps_e_greedy = v

    def get_greedy_action(self, im_tensor, vec_tensor):
        q_val = self.qnet(im_tensor, vec_tensor).detach()
        _, index = q_val.topk(1)
        return index[0], q_val

    def obs_to_tensor(self, raw_observation):
        im_tensor = torch.tensor(raw_observation[0]).view((
            1,
            self.shape_obs_image[2],
            self.shape_obs_image[0],
            self.shape_obs_image[1]
        ))
        vec_tensor = torch.tensor(raw_observation[1])
        return im_tensor, vec_tensor

    def save_network(self, n_experiment: int):
        torch.save(self.qnet.state_dict(),
                   f=f"saved_network/qnet_{n_experiment}.pth")
        print(f"Network saved in {n_experiment}-th experiment.")
