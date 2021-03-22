import contextlib
import joblib
import numpy as np
import torch
import os
import warnings

from collections import Iterable
from itertools import chain
from joblib import Parallel, delayed
from matplotlib import pyplot as plt
from time import sleep
from tqdm.auto import tqdm

from .rollout import rollout


def plot_sample(data, axarr, colors=None, **kwargs):
    H, D = data.shape
    plots = []
    if colors is None:
        colors = ['steelblue'] * D
    N = len(colors)
    for d in range(D):
        pl, = axarr[d].plot(np.arange(H),
                            data[:, d],
                            color=colors[d % N],
                            **kwargs)
        plots.append(pl)
    return plots


def plot_mean_var(data, axarr, colors=None, stdevs=2, **kwargs):
    N, H, D = data.shape
    plots = []
    mean = data.mean(0)
    std = data.std(0)
    t = np.arange(H)
    if colors is None:
        colors = ['steelblue'] * D
    N = len(colors)
    for d in range(D):
        pl, = axarr[d].plot(t, mean[:, d], color=colors[d % N], **kwargs)
        alpha = kwargs.get('alpha', 0.5)
        for i in range(1, stdevs + 1):
            alpha = alpha * 0.8
            lower_bound = mean[:, d] - i * std[:, d]
            upper_bound = mean[:, d] + i * std[:, d]
            axarr[d].fill_between(t,
                                  lower_bound,
                                  upper_bound,
                                  alpha=alpha,
                                  color=pl.get_color())
        plots.append(pl)
    return plots


def plot_trajectories(
        states,
        actions,
        rewards,
        names=['Rolled out States', 'Predicted Actions', 'Predicted Rewards'],
        timeout=0.5,
        plot_samples=True):
    for name in names:
        fig = plt.figure(name)
        fig.clear()

    fig1, axarr1 = plt.subplots(states.shape[-1],
                                num=names[0],
                                sharex=True,
                                figsize=(16, 9))
    fig2, axarr2 = plt.subplots(actions.shape[-1],
                                num=names[1],
                                sharex=True,
                                figsize=(16, 3))
    fig3, axarr3 = plt.subplots(rewards.shape[-1],
                                num=names[2],
                                sharex=True,
                                figsize=(16, 3))

    axarr1 = [axarr1] if not isinstance(axarr1, Iterable) else axarr1
    axarr2 = [axarr2] if not isinstance(axarr2, Iterable) else axarr2
    axarr3 = [axarr3] if not isinstance(axarr3, Iterable) else axarr3
    if plot_samples:
        c1 = c2 = c3 = None
        for i, (st, ac, rw) in enumerate(zip(states, actions, rewards)):
            r1 = plot_sample(st, axarr1, c1, alpha=0.3)
            r2 = plot_sample(ac, axarr2, c2, alpha=0.3)
            r3 = plot_sample(rw, axarr3, c3, alpha=0.3)
            c1 = [r.get_color() for r in r1]
            c2 = [r.get_color() for r in r2]
            c3 = [r.get_color() for r in r3]

    else:
        plot_mean_var(states, axarr1)
        plot_mean_var(actions, axarr2)
        plot_mean_var(rewards, axarr3)

    for ax in chain(axarr1, axarr2, axarr3):
        ax.figure.canvas.draw()

    if timeout > 0:
        plt.show(block=False)
        plt.waitforbuttonpress(timeout)
    else:
        plt.show()


def plot_rollout(x0, forward, pol, steps):
    trajs = rollout(x0,
                    forward,
                    pol,
                    steps,
                    resample_model=False,
                    resample_policy=False,
                    resample_particles=False)
    states, actions, rewards = (torch.stack(x).transpose(
        0, 1).detach().cpu().numpy() for x in trajs[:3])
    plot_trajectories(states, actions, rewards)


def jacobian(y, x, **kwargs):
    """Evaluates the jacobian of y w.r.t x safely.

    Args:
        y (Tensor<m>): Tensor to differentiate.
        x (Tensor<n>): Tensor to differentiate with respect to.
        **kwargs: Additional key-word arguments to pass to `grad()`.

    Returns:
        Jacobian (Tensor<m, n>).
    """
    J = [torch.autograd.grad(y[i], x, **kwargs)[0] for i in range(y.shape[0])]
    J = torch.stack(J)
    J.requires_grad_()
    return J


def batch_jacobian(f, x, out_dims=None):
    if out_dims is None:
        y = f(x)
        out_dims = y.shape[-1]
    x_rep = x.repeat(out_dims, 1)
    x_rep = torch.tensor(x_rep, requires_grad=True)
    y_rep = f(x_rep)
    dydx = torch.autograd.grad(y_rep,
                               x_rep,
                               torch.eye(x.shape[-1]),
                               allow_unused=True,
                               retain_graph=True)
    return dydx


def polyak_averaging(current, target, tau=0.005):
    for param, target_param in zip(current.parameters(), target.parameters()):
        target_param.data.copy_(tau * param.data +
                                (1 - tau) * target_param.data)


def perturb_initial_action(i, states, actions):
    if i == 0:
        actions = actions + 1e-1 * (torch.randint(0,
                                                  2,
                                                  actions.shape[0:],
                                                  device=actions.device,
                                                  dtype=actions.dtype) *
                                    actions.std(0)).detach()
    return states, actions


def threshold_linear(x, y0, yend, x0, xend):
    y = (x - x0) * (yend - y0) / (xend - x0) + y0
    return np.maximum(y0, np.minimum(yend, y)).astype(np.int32)


def sin_squashing_fn(x):
    '''
        Periodic squashing function from PILCO.
        Bounds the output to be between -1 and 1
    '''
    xx = torch.stack([x, 3 * x]).sin()
    scale = torch.tensor([9.0, 1.0], device=x.device,
                         dtype=x.dtype)[[None] * x.dim()].transpose(0, -1)
    return 0.125 * (xx * scale).sum(0)


def tile(tensor, n):
    return tensor.unsqueeze(0).transpose(0, 1).repeat(1, 1, n).view(
        n * tensor.shape[0], -1)


def load_csv(s):
    try:
        return [int(d) for d in s.split(',')]
    except Exception:
        return None


def load_checkpoint(path, dyn, pol, exp, val=None):
    msg = "Unable to load dynamics model parameters at {}"
    try:
        dyn_params = torch.load(os.path.join(path, 'latest_dynamics.pth.tar'))
        dyn.load(dyn_params)
    except Exception:
        warnings.warn(msg.format(path, "latest_dynamics.pth.tar"))

    try:
        pol_params = torch.load(os.path.join(path, 'latest_policy.pth.tar'))
        pol.load(pol_params)
    except Exception:
        warnings.warn(msg.format(path, "latest_policy.pth.tar"))

    if val is not None:
        try:
            val_path = os.path.join(path, 'latest_critic.pth.tar')
            val_params = torch.load(val_path)
            val.load(val_params)
        except Exception:
            warnings.warn(msg.format(val_path))

    try:
        exp_path = os.path.join(path, 'experience.pth.tar')
        exp.load(exp_path)
    except Exception:
        warnings.warn(msg.format(exp_path))


def train_model(model,
                X,
                Y,
                n_iters=10000,
                opt=None,
                resample=True,
                loss=None,
                batch_size=100):
    import gc
    model.train()
    # setup default optimizer if none available
    if opt is None:
        opt = torch.optim.Adam(model.parameters(), lr=1e-3)

    # setup dataloader
    dataloader = torch.utils.data.DataLoader(torch.utils.data.TensorDataset(
        X, Y),
        batch_size=batch_size,
        shuffle=True,
        num_workers=0)
    data_iter = iter(dataloader)

    def next_batch():
        # get next batch
        try:
            x, y = next(data_iter)
        except BaseException:
            data_iter = iter(dataloader)
            x, y = next(data_iter)
        return x, y

    # train model
    pbar = tqdm(range(n_iters))
    for i in pbar:
        opt.zero_grad()
        x, y = next_batch()
        pygx, dist_params = model(x, resample=resample)
        ll = pygx.log_prob(y).mean()
        reg = model.regularization_loss()
        loss = -ll + reg / X.shape[0]
        loss.backward()
        opt.step()
        pbar.set_description(f'data logL: {ll.detach().cpu().numpy()}')
        if i % 500 == 0:
            gc.collect()
            torch.cuda.empty_cache()
    model.eval()


@contextlib.contextmanager
def tqdm_joblib(tqdm_object):
    """Context manager to patch joblib to report into tqdm progress bar given as argument"""
    class TqdmBatchCompletionCallback(joblib.parallel.BatchCompletionCallBack):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        def __call__(self, *args, **kwargs):
            tqdm_object.update(n=self.batch_size)
            return super().__call__(*args, **kwargs)

    old_batch_callback = joblib.parallel.BatchCompletionCallBack
    joblib.parallel.BatchCompletionCallBack = TqdmBatchCompletionCallback
    try:
        yield tqdm_object
    finally:
        joblib.parallel.BatchCompletionCallBack = old_batch_callback
        tqdm_object.close()
