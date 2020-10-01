# Copyright (C) 2018, Anass Al, Juan Camilo Gamboa Higuera
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>
"""Cartpole environment."""

import torch
import numpy as np

from gym import spaces

from .model import CartpoleModel
from ..base import GymEnv
from ...utils import angles


class CartpoleReward(torch.nn.Module):
    def __init__(self,
                 pole_length=0.5,
                 target=torch.tensor([0, 0, np.pi, 0]),
                 Q=16.0 * torch.eye(2),
                 R=1e-4 * torch.eye(1)):
        super(CartpoleReward, self).__init__()
        self.Q = torch.nn.Parameter(Q, requires_grad=False)
        self.R = torch.nn.Parameter(R, requires_grad=False)
        if target.dim() == 1:
            target = target.unsqueeze(0)
        self.target = torch.nn.Parameter(target, requires_grad=False)
        self.pole_length = torch.nn.Parameter(pole_length, requires_grad=False)

    def forward(self, x, u):
        if not isinstance(x, torch.Tensor):
            x = torch.tensor(x)
        if not isinstance(u, torch.Tensor):
            u = torch.tensor(u)
        x = x.to(device=self.Q.device, dtype=self.Q.dtype)
        u = u.to(device=self.Q.device, dtype=self.Q.dtype)
        if x.dim() == 1:
            x = x.unsqueeze(0)
        if u.dim() == 1:
            u = u.unsqueeze(0)
        # compute the distance between the tip of the pole and the target tip
        # location
        targeta = angles.to_complex(self.target, [2])
        target_tip_xy = torch.cat([
            targeta[:, 0, None] + self.pole_length * targeta[:, 3, None],
            -self.pole_length * targeta[:, 4, None]
        ],
                                  dim=-1)
        if x.shape[-1] != targeta.shape[-1]:
            xa = angles.to_complex(x, [2])
        else:
            xa = x
        pole_tip_xy = torch.cat([
            xa[:, 0, None] + self.pole_length * xa[:, 3, None],
            -self.pole_length * xa[:, 4, None]
        ],
                                dim=-1)

        pole_tip_xy = pole_tip_xy.unsqueeze(
            0) if pole_tip_xy.dim() == 1 else pole_tip_xy
        target_tip_xy = target_tip_xy.unsqueeze(
            0) if target_tip_xy.dim() == 1 else target_tip_xy

        # normalized distance so that cost at [0 ,0 ,0, 0] is 1
        delta = (pole_tip_xy - target_tip_xy)
        delta = delta / (2 * self.pole_length)

        # compute cost
        cost = 0.5 * ((delta.mm(self.Q) * delta).sum(-1, keepdim=True) +
                      (u.mm(self.R) * u).sum(-1, keepdim=True))

        # reward is negative cost.
        # optimizing the exponential of the negative cost
        reward = (-cost).exp()
        return reward


class CartpoleReward2(torch.nn.Module):
    def __init__(self):
        super(CartpoleReward, self).__init__()

    def forward(self, x, u, k1=1.0, k2=0.2, w=1.0, v=1.0, a=0.1):
        angle = x[..., 2]
        pos = x[..., 0]
        if isinstance(x, torch.Tensor):
            rc = pos.abs()
            rp = w * angle**2 + v * (angle + a).log()
        else:
            rc = np.abs(pos)
            rp = w * angle**2 + v * np.log(angle + a)

        return -k1 * rp - k2 * rc + 10.0


class Cartpole(GymEnv):
    """Open AI gym cartpole environment.

    Based on the OpenAI gym CartPole-v0 environment, but with full-swing up
    support and a continuous action-space.
    """

    metadata = {
        "render.modes": ["human", "rgb_array"],
        "video.frames_per_second": 50,
    }

    def __init__(self, model=None, reward_func=None, **kwargs):
        if model is None:
            model = CartpoleModel()
        # init parent class
        reward_func = reward_func if callable(reward_func) else CartpoleReward(
            pole_length=model.lp)
        measurement_noise = torch.tensor([0.01] * 4)
        super(Cartpole, self).__init__(model,
                                       reward_func,
                                       measurement_noise,
                                       angle_dims=[2],
                                       **kwargs)

        # init this class
        high = np.array([10])
        self.action_space = spaces.Box(-high, high, dtype=np.float32)

        high = np.array([
            4,
            10,
            2 * np.pi,
            10,
        ], dtype=np.float32)
        if self.angle_dims is not None:
            rnd = torch.distributions.Uniform(torch.tensor(-high),
                                              torch.tensor(high)).sample(
                                                  [1000])
            low = angles.to_complex(rnd,
                                    self.angle_dims).min(0)[0].float().numpy()
            high = angles.to_complex(
                rnd, self.angle_dims).max(0)[0].float().numpy()
        else:
            low = -high

        self.observation_space = spaces.Box(low=low,
                                            high=high,
                                            dtype=np.float32)

    def step(self,
             action,
             x_lim=[-3.5, 3.5],
             ang_lim=[-4 * np.pi, 4 * np.pi],
             **kwargs):
        state, reward, done, info = super(Cartpole,
                                          self).step(action, **kwargs)
        if self.state[0] < x_lim[0] or self.state[0] > x_lim[1]:
            done = True
        if self.state[2] < ang_lim[0] or self.state[2] > ang_lim[1]:
            done = True
        return state, reward, done, info

    def reset(self,
              init_state=np.array([0.0, 0.0, 0.0, 0.0]),
              init_state_std=1e-1):
        return super(Cartpole, self).reset(init_state, init_state_std)

    def render(self, mode="human", N=1):
        N = max(1, N)
        screen_width = 600
        screen_height = 600

        world_width = 2.5
        scale = screen_width / world_width
        carty = 100  # TOP OF CART
        polewidth = 10.0 * (self.model.mp / 0.5)**0.5
        polelen = scale * self.model.lp
        cartwidth = 50.0 * (self.model.mc / 0.5)**0.25
        cartheight = 30.0 * (self.model.mc / 0.5)**0.25

        if self.state is None:
            return None

        x, _, theta, _ = self.state
        cartx = x * scale + screen_width / 2.0  # MIDDLE OF CART

        if self.viewer is None:
            from gym.envs.classic_control import rendering

            self.viewer = rendering.Viewer(screen_width, screen_height)
            self.viewer.window.set_vsync(False)

            self.carttrans = [0] * N
            self.poletrans = [0] * N
            self.axles = [0] * N

            for i in range(N - 1, -1, -1):
                l, r, t, b = (-cartwidth / 2, cartwidth / 2, cartheight / 2,
                              -cartheight / 2)
                axleoffset = cartheight / 4.0
                cart = rendering.FilledPolygon([(l, b), (l, t), (r, t),
                                                (r, b)])
                cart.attrs[0].vec4 = (0.0, 0.0, 0.0, 1.0 / (N - i))
                self.carttrans[i] = rendering.Transform()
                cart.add_attr(self.carttrans[i])
                self.viewer.add_geom(cart)

                l, r, t, b = (-polewidth / 2, polewidth / 2,
                              polelen - polewidth / 2, -polewidth / 2)
                pole = rendering.FilledPolygon([(l, b), (l, t), (r, t),
                                                (r, b)])
                pole.set_color(0.8, 0.6, 0.4)
                pole.attrs[0].vec4 = (0.8, 0.6, 0.4, 1.0 / (N - i))
                self.poletrans[i] = rendering.Transform(
                    translation=(0, axleoffset))
                pole.add_attr(self.poletrans[i])
                pole.add_attr(self.carttrans[i])
                self.viewer.add_geom(pole)

                self.axles[i] = rendering.make_circle(polewidth / 2)
                self.axles[i].add_attr(self.poletrans[i])
                self.axles[i].add_attr(self.carttrans[i])
                self.axles[i].set_color(0.5, 0.5, 0.8)
                self.axles[i].attrs[0].vec4 = (0.5, 0.5, 0.8, 1.0 / (N - i))
                self.viewer.add_geom(self.axles[i])

                self.carttrans[i].set_translation(cartx, carty)
                self.poletrans[i].set_rotation(-theta - np.pi)

            self.track = rendering.Line((0, carty), (screen_width, carty))
            self.track.set_color(0, 0, 0)
            self.viewer.add_geom(self.track)

        for i in range(N - 1):
            self.carttrans[i].set_translation(*self.carttrans[i +
                                                              1].translation)
            self.poletrans[i].set_rotation(self.poletrans[i + 1].rotation)

        self.carttrans[-1].set_translation(cartx, carty)
        self.poletrans[-1].set_rotation(theta + np.pi)

        return self.viewer.render(return_rgb_array=mode == "rgb_array")

    def close(self):
        if self.viewer:
            self.viewer.close()
