import os
import queue
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import List, NamedTuple, Optional, Sequence, Tuple
from functools import partial

from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv
from red_gym_env import RedGymEnv
from pathlib import Path

import flax
import flax.linen as nn
import jax
import jax.numpy as jnp
import numpy as np
import optax
import tyro
from flax.linen.initializers import constant, orthogonal
from flax.training.train_state import TrainState
from rich.pretty import pprint
from tensorboardX import SummaryWriter

# Fix weird OOM https://github.com/google/jax/discussions/6332#discussioncomment-1279991
os.environ["XLA_PYTHON_CLIENT_MEM_FRACTION"] = "0.6"
os.environ["XLA_FLAGS"] = "--xla_cpu_multi_thread_eigen=false " "intra_op_parallelism_threads=1"
# Fix CUDNN non-determinisim; https://github.com/google/jax/issues/4823#issuecomment-952835771
os.environ["TF_XLA_FLAGS"] = "--xla_gpu_autotune_level=2 --xla_gpu_deterministic_reductions"
os.environ["TF_CUDNN DETERMINISTIC"] = "1"


@dataclass
class Args:
    exp_name: str = os.path.basename(__file__).rstrip(".py")
    "the name of this experiment"
    seed: int = 1
    "seed of the experiment"
    track: bool = False
    "if toggled, this experiment will be tracked with Weights and Biases"
    wandb_project_name: str = "cleanRL"
    "the wandb's project name"
    wandb_entity: str = None
    "the entity (team) of wandb's project"
    capture_video: bool = False
    "whether to capture videos of the agent performances (check out `videos` folder)"
    save_model: bool = False
    "whether to save model into the `runs/{run_name}` folder"
    upload_model: bool = False
    "whether to upload the saved model to huggingface"
    hf_entity: str = ""
    "the user or org name of the model repository from the Hugging Face Hub"
    log_frequency: int = 10
    "the logging frequency of the model performance (in terms of `updates`)"

    # Algorithm specific arguments
    env_id: str = "Breakout-v5"
    "the id of the environment"
    total_timesteps: int = 50000000
    "total timesteps of the experiments"
    learning_rate: float = 2.5e-4
    "the learning rate of the optimizer"
    local_num_envs: int = 64
    "the number of parallel game environments"
    num_actor_threads: int = 2
    "the number of actor threads to use"
    num_steps: int = 128
    "the number of steps to run in each environment per policy rollout"
    anneal_lr: bool = True
    "Toggle learning rate annealing for policy and value networks"
    gamma: float = 0.99
    "the discount factor gamma"
    gae_lambda: float = 0.95
    "the lambda for the general advantage estimation"
    num_minibatches: int = 4
    "the number of mini-batches"
    gradient_accumulation_steps: int = 1
    "the number of gradient accumulation steps before performing an optimization step"
    update_epochs: int = 4
    "the K epochs to update the policy"
    norm_adv: bool = True
    "Toggles advantages normalization"
    clip_coef: float = 0.1
    "the surrogate clipping coefficient"
    ent_coef: float = 0.01
    "coefficient of the entropy"
    vf_coef: float = 0.5
    "coefficient of the value function"
    max_grad_norm: float = 0.5
    "the maximum norm for the gradient clipping"
    channels: List[int] = field(default_factory=lambda: [16, 32, 32])
    "the channels of the CNN"
    hiddens: List[int] = field(default_factory=lambda: [256])
    "the hiddens size of the MLP"

    actor_device_ids: List[int] = field(default_factory=lambda: [0])
    "the device ids that actor workers will use"
    learner_device_ids: List[int] = field(default_factory=lambda: [0])
    "the device ids that learner workers will use"
    distributed: bool = False
    "whether to use `jax.distirbuted`"
    concurrency: bool = False
    "whether to run the actor and learner concurrently"

    # runtime arguments to be filled in
    local_batch_size: int = 0
    local_minibatch_size: int = 0
    num_updates: int = 0
    world_size: int = 0
    local_rank: int = 0
    num_envs: int = 0
    batch_size: int = 0
    minibatch_size: int = 0
    num_updates: int = 0
    global_learner_decices: Optional[List[str]] = None
    actor_devices: Optional[List[str]] = None
    learner_devices: Optional[List[str]] = None


ATARI_MAX_FRAMES = int(
    108000 / 4
)  # 108000 is the max number of frames in an Atari game, divided by 4 to account for frame skipping


ep_length = 2048 * 10
sess_path = Path(f'session_{str(uuid.uuid4())[:8]}')
env_config = {
            'headless': True, 'save_final_state': True, 'early_stop': False,
            'action_freq': 24, 'init_state': '../has_pokedex_nballs.state', 'max_steps': ep_length, 
            'print_rewards': True, 'save_video': False, 'fast_video': True, 'session_path': sess_path,
            'gb_path': '../PokemonRed.gb', 'debug': False, 'sim_frame_dist': 2_000_000.0, 
            'use_screen_explore': True, 'reward_scale': 4, 'extra_buttons': False,
            'explore_weight': 3 # 2.5
        }
def make_poke_env(rank, env_conf, seed=0):
    """
    Utility function for multiprocessed env.
    :param env_id: (str) the environment ID
    :param num_env: (int) the number of environments you wish to have in subprocesses
    :param seed: (int) the initial seed for RNG
    :param rank: (int) index of the subprocess
    """
    def _init():
        env = RedGymEnv(env_conf)
        env.reset(seed=(seed + rank))
        return env
    return _init

def make_env(env_id, seed, num_envs):
    def thunk():
        envs = SubprocVecEnv([make_poke_env(i, env_config, seed) for i in range(num_envs)])
        envs.num_envs = num_envs
        envs.single_action_space = envs.action_space
        envs.single_observation_space = envs.observation_space
        envs.is_vector_env = True
        return envs

    return thunk


class ResidualBlock(nn.Module):
    channels: int

    @nn.compact
    def __call__(self, x):
        inputs = x
        x = nn.relu(x)
        x = nn.Conv(self.channels, kernel_size=(3, 3))(x)
        x = nn.relu(x)
        x = nn.Conv(self.channels, kernel_size=(3, 3))(x)
        return x + inputs


class ConvSequence(nn.Module):
    channels: int

    @nn.compact
    def __call__(self, x):
        x = nn.Conv(self.channels, kernel_size=(3, 3))(x)
        x = nn.max_pool(x, window_shape=(3, 3), strides=(2, 2), padding="SAME")
        x = ResidualBlock(self.channels)(x)
        x = ResidualBlock(self.channels)(x)
        return x


class Network(nn.Module):
    channels: Sequence[int] = (16, 32, 32)
    hiddens: Sequence[int] = (256,)

    @nn.compact
    def __call__(self, x):
        x = jnp.transpose(x, (0, 2, 3, 1))
        x = x / (255.0)
        for channels in self.channels:
            x = ConvSequence(channels)(x)
        x = nn.relu(x)
        x = x.reshape((x.shape[0], -1))
        for hidden in self.hiddens:
            x = nn.Dense(hidden, kernel_init=orthogonal(np.sqrt(2)), bias_init=constant(0.0))(x)
            x = nn.relu(x)
        return x


class Critic(nn.Module):
    @nn.compact
    def __call__(self, x):
        return nn.Dense(1, kernel_init=orthogonal(1), bias_init=constant(0.0))(x)


class Actor(nn.Module):
    action_dim: int

    @nn.compact
    def __call__(self, x):
        return nn.Dense(self.action_dim, kernel_init=orthogonal(0.01), bias_init=constant(0.0))(x)


@flax.struct.dataclass
class AgentParams:
    network_params: flax.core.FrozenDict
    actor_params: flax.core.FrozenDict
    critic_params: flax.core.FrozenDict


class Transition(NamedTuple):
    obs: list
    dones: list
    actions: list
    logprobs: list
    values: list
    # env_ids: list
    rewards: list
    # truncations: list
    # terminations: list
    firststeps: list  # first step of an episode


def rollout(
    key: jax.random.PRNGKey,
    args,
    rollout_queue,
    params_queue: queue.Queue,
    writer,
    learner_devices,
    device_thread_id,
    actor_device,
):
    envs = make_env(
        args.env_id,
        args.seed + jax.process_index() + device_thread_id,
        args.local_num_envs,
    )()
    len_actor_device_ids = len(args.actor_device_ids)
    global_step = 0
    start_time = time.time()

    @jax.jit
    def get_action_and_value(
        params: flax.core.FrozenDict,
        next_obs: np.ndarray,
        key: jax.random.PRNGKey,
    ):
        next_obs = jnp.array(next_obs)
        hidden = Network(args.channels, args.hiddens).apply(params.network_params, next_obs)
        logits = Actor(envs.single_action_space.n).apply(params.actor_params, hidden)
        # sample action: Gumbel-softmax trick
        # see https://stats.stackexchange.com/questions/359442/sampling-from-a-categorical-distribution
        key, subkey = jax.random.split(key)
        u = jax.random.uniform(subkey, shape=logits.shape)
        action = jnp.argmax(logits - jnp.log(-jnp.log(u)), axis=1)
        logprob = jax.nn.log_softmax(logits)[jnp.arange(action.shape[0]), action]
        value = Critic().apply(params.critic_params, hidden)
        return next_obs, action, logprob, value.squeeze(), key

    # put data in the last index
    episode_returns = np.zeros((args.local_num_envs,), dtype=np.float32)
    returned_episode_returns = np.zeros((args.local_num_envs,), dtype=np.float32)
    episode_lengths = np.zeros((args.local_num_envs,), dtype=np.float32)
    returned_episode_lengths = np.zeros((args.local_num_envs,), dtype=np.float32)

    params_queue_get_time = deque(maxlen=10)
    rollout_time = deque(maxlen=10)
    rollout_queue_put_time = deque(maxlen=10)
    actor_policy_version = 0
    next_obs = envs.reset()
    next_done = jnp.zeros(args.local_num_envs, dtype=jax.numpy.bool_)

    @jax.jit
    def prepare_data(storage: List[Transition]) -> Transition:
        return jax.tree_map(lambda *xs: jnp.split(jnp.stack(xs), len(learner_devices), axis=1), *storage)

    for update in range(1, args.num_updates + 2):
        update_time_start = time.time()
        env_recv_time = 0
        inference_time = 0
        storage_time = 0
        d2h_time = 0
        env_send_time = 0
        # NOTE: `update != 2` is actually IMPORTANT — it allows us to start running policy collection
        # concurrently with the learning process. It also ensures the actor's policy version is only 1 step
        # behind the learner's policy version
        params_queue_get_time_start = time.time()
        if args.concurrency:
            if update != 2:
                params = params_queue.get()
                # NOTE: block here is important because otherwise this thread will call
                # the jitted `get_action_and_value` function that hangs until the params are ready.
                # This blocks the `get_action_and_value` function in other actor threads.
                # See https://excalidraw.com/#json=hSooeQL707gE5SWY8wOSS,GeaN1eb2r24PPi75a3n14Q for a visual explanation.
                params.network_params["params"]["Dense_0"][
                    "kernel"
                ].block_until_ready()  # TODO: check if params.block_until_ready() is enough
                actor_policy_version += 1
        else:
            params = params_queue.get()
            actor_policy_version += 1
        params_queue_get_time.append(time.time() - params_queue_get_time_start)
        rollout_time_start = time.time()
        storage = []
        for _ in range(0, args.num_steps):
            cached_next_obs = next_obs
            cached_next_done = next_done
            global_step += len(next_done) * args.num_actor_threads * len_actor_device_ids * args.world_size
            inference_time_start = time.time()
            cached_next_obs, action, logprob, value, key = get_action_and_value(params, cached_next_obs, key)
            inference_time += time.time() - inference_time_start

            d2h_time_start = time.time()
            cpu_action = np.array(action)
            d2h_time += time.time() - d2h_time_start

            env_send_time_start = time.time()
            next_obs, next_reward, next_done, info = envs.step(cpu_action)
            env_send_time += time.time() - env_send_time_start
            storage_time_start = time.time()

            # info["TimeLimit.truncated"] has a bug https://github.com/sail-sg/envpool/issues/239
            # so we use our own truncated flag
            # truncated = info["elapsed_step"] >= envs.spec.config.max_episode_steps
            storage.append(
                Transition(
                    obs=cached_next_obs,
                    dones=cached_next_done,
                    actions=action,
                    logprobs=logprob,
                    values=value,
                    rewards=next_reward,
                    # truncations=truncated,
                    # terminations=next_done,
                    # firststeps=info["elapsed_step"] == 0,
                    firststeps=cached_next_done,
                )
            )
            episode_returns += next_reward
            returned_episode_returns = np.where(
                next_done, episode_returns, returned_episode_returns
            )
            episode_returns *= (1 - next_done)
            episode_lengths += 1
            returned_episode_lengths = np.where(
                next_done, episode_lengths, returned_episode_lengths
            )
            episode_lengths *= (1 - next_done)
            storage_time += time.time() - storage_time_start
        rollout_time.append(time.time() - rollout_time_start)

        avg_episodic_return = np.mean(returned_episode_returns)
        partitioned_storage = prepare_data(storage)
        sharded_storage = Transition(
            *list(map(lambda x: jax.device_put_sharded(x, devices=learner_devices), partitioned_storage))
        )
        # next_obs, next_done are still in the host
        sharded_next_obs = jax.device_put_sharded(np.split(next_obs, len(learner_devices)), devices=learner_devices)
        sharded_next_done = jax.device_put_sharded(np.split(next_done, len(learner_devices)), devices=learner_devices)
        payload = (
            global_step,
            actor_policy_version,
            update,
            sharded_storage,
            sharded_next_obs,
            sharded_next_done,
            np.mean(params_queue_get_time),
            device_thread_id,
        )
        rollout_queue_put_time_start = time.time()
        rollout_queue.put(payload)
        rollout_queue_put_time.append(time.time() - rollout_queue_put_time_start)

        if update % args.log_frequency == 0:
            if device_thread_id == 0:
                print(
                    f"global_step={global_step}, avg_episodic_return={avg_episodic_return}, rollout_time={np.mean(rollout_time)}"
                )
                print("SPS:", int(global_step / (time.time() - start_time)))
            writer.add_scalar("stats/rollout_time", np.mean(rollout_time), global_step)
            writer.add_scalar("charts/avg_episodic_return", avg_episodic_return, global_step)
            writer.add_scalar("charts/avg_episodic_length", np.mean(returned_episode_lengths), global_step)
            writer.add_scalar("stats/params_queue_get_time", np.mean(params_queue_get_time), global_step)
            writer.add_scalar("stats/env_recv_time", env_recv_time, global_step)
            writer.add_scalar("stats/inference_time", inference_time, global_step)
            writer.add_scalar("stats/storage_time", storage_time, global_step)
            writer.add_scalar("stats/d2h_time", d2h_time, global_step)
            writer.add_scalar("stats/env_send_time", env_send_time, global_step)
            writer.add_scalar("stats/rollout_queue_put_time", np.mean(rollout_queue_put_time), global_step)
            writer.add_scalar("charts/SPS", int(global_step / (time.time() - start_time)), global_step)
            writer.add_scalar(
                "charts/SPS_update",
                int(
                    args.local_num_envs
                    * args.num_steps
                    * len_actor_device_ids
                    * args.num_actor_threads
                    * args.world_size
                    / (time.time() - update_time_start)
                ),
                global_step,
            )


if __name__ == "__main__":
    args = tyro.cli(Args)
    args.local_batch_size = int(args.local_num_envs * args.num_steps * args.num_actor_threads * len(args.actor_device_ids))
    args.local_minibatch_size = int(args.local_batch_size // args.num_minibatches)
    assert (
        args.local_num_envs % len(args.learner_device_ids) == 0
    ), "local_num_envs must be divisible by len(learner_device_ids)"
    assert (
        int(args.local_num_envs / len(args.learner_device_ids)) * args.num_actor_threads % args.num_minibatches == 0
    ), "int(local_num_envs / len(learner_device_ids)) must be divisible by num_minibatches"
    if args.distributed:
        jax.distributed.initialize(
            local_device_ids=range(len(args.learner_device_ids) + len(args.actor_device_ids)),
        )
        print(list(range(len(args.learner_device_ids) + len(args.actor_device_ids))))

    args.world_size = jax.process_count()
    args.local_rank = jax.process_index()
    args.num_envs = args.local_num_envs * args.world_size * args.num_actor_threads * len(args.actor_device_ids)
    args.batch_size = args.local_batch_size * args.world_size
    args.minibatch_size = args.local_minibatch_size * args.world_size
    args.num_updates = args.total_timesteps // (args.local_batch_size * args.world_size)
    local_devices = jax.local_devices()
    global_devices = jax.devices()
    learner_devices = [local_devices[d_id] for d_id in args.learner_device_ids]
    actor_devices = [local_devices[d_id] for d_id in args.actor_device_ids]
    global_learner_decices = [
        global_devices[d_id + process_index * len(local_devices)]
        for process_index in range(args.world_size)
        for d_id in args.learner_device_ids
    ]
    print("global_learner_decices", global_learner_decices)
    args.global_learner_decices = [str(item) for item in global_learner_decices]
    args.actor_devices = [str(item) for item in actor_devices]
    args.learner_devices = [str(item) for item in learner_devices]
    pprint(args)

    run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{uuid.uuid4()}"
    if args.track and args.local_rank == 0:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    key = jax.random.PRNGKey(args.seed)
    key, network_key, actor_key, critic_key = jax.random.split(key, 4)
    learner_keys = jax.device_put_replicated(key, learner_devices)

    # env setup
    envs = make_env(args.env_id, args.seed, args.local_num_envs)()

    def linear_schedule(count):
        # anneal learning rate linearly after one training iteration which contains
        # (args.num_minibatches) gradient updates
        frac = 1.0 - (count // (args.num_minibatches * args.update_epochs)) / args.num_updates
        return args.learning_rate * frac

    network = Network(args.channels, args.hiddens)
    actor = Actor(action_dim=envs.single_action_space.n)
    critic = Critic()
    network_params = network.init(network_key, np.array([envs.single_observation_space.sample()]))
    agent_state = TrainState.create(
        apply_fn=None,
        params=AgentParams(
            network_params,
            actor.init(actor_key, network.apply(network_params, np.array([envs.single_observation_space.sample()]))),
            critic.init(critic_key, network.apply(network_params, np.array([envs.single_observation_space.sample()]))),
        ),
        tx=optax.MultiSteps(
            optax.chain(
                optax.clip_by_global_norm(args.max_grad_norm),
                optax.inject_hyperparams(optax.adam)(
                    learning_rate=linear_schedule if args.anneal_lr else args.learning_rate, eps=1e-5
                ),
            ),
            every_k_schedule=args.gradient_accumulation_steps,
        ),
    )
    agent_state = flax.jax_utils.replicate(agent_state, devices=learner_devices)
    print(network.tabulate(network_key, np.array([envs.single_observation_space.sample()])))
    print(actor.tabulate(actor_key, network.apply(network_params, np.array([envs.single_observation_space.sample()]))))
    print(critic.tabulate(critic_key, network.apply(network_params, np.array([envs.single_observation_space.sample()]))))

    @jax.jit
    def get_value(
        params: flax.core.FrozenDict,
        obs: np.ndarray,
    ):
        hidden = Network(args.channels, args.hiddens).apply(params.network_params, obs)
        value = Critic().apply(params.critic_params, hidden).squeeze(-1)
        return value

    @jax.jit
    def get_logprob_entropy_value(
        params: flax.core.FrozenDict,
        obs: np.ndarray,
        actions: np.ndarray,
    ):
        hidden = Network(args.channels, args.hiddens).apply(params.network_params, obs)
        logits = Actor(envs.single_action_space.n).apply(params.actor_params, hidden)
        logprob = jax.nn.log_softmax(logits)[jnp.arange(actions.shape[0]), actions]
        logits = logits - jax.scipy.special.logsumexp(logits, axis=-1, keepdims=True)
        logits = logits.clip(min=jnp.finfo(logits.dtype).min)
        p_log_p = logits * jax.nn.softmax(logits)
        entropy = -p_log_p.sum(-1)
        value = Critic().apply(params.critic_params, hidden).squeeze(-1)
        return logprob, entropy, value

    def compute_gae_once(carry, inp, gamma, gae_lambda):
        advantages = carry
        nextdone, nextvalues, curvalues, reward = inp
        nextnonterminal = 1.0 - nextdone

        delta = reward + gamma * nextvalues * nextnonterminal - curvalues
        advantages = delta + gamma * gae_lambda * nextnonterminal * advantages
        return advantages, advantages

    compute_gae_once = partial(compute_gae_once, gamma=args.gamma, gae_lambda=args.gae_lambda)

    @jax.jit
    def compute_gae(
        agent_state: TrainState,
        next_obs: np.ndarray,
        next_done: np.ndarray,
        storage: Transition,
    ):
        next_value = critic.apply(
            agent_state.params.critic_params, network.apply(agent_state.params.network_params, next_obs)
        ).squeeze()

        advantages = jnp.zeros_like(next_value)
        dones = jnp.concatenate([storage.dones, next_done[None, :]], axis=0)
        values = jnp.concatenate([storage.values, next_value[None, :]], axis=0)
        _, advantages = jax.lax.scan(
            compute_gae_once, advantages, (dones[1:], values[1:], values[:-1], storage.rewards), reverse=True
        )
        return advantages, advantages + storage.values

    def ppo_loss(params, obs, actions, behavior_logprobs, firststeps, advantages, target_values):
        newlogprob, entropy, newvalue = get_logprob_entropy_value(params, obs, actions)
        logratio = newlogprob - behavior_logprobs
        ratio = jnp.exp(logratio)
        approx_kl = ((ratio - 1) - logratio).mean()

        # Policy loss
        pg_loss1 = -advantages * ratio
        pg_loss2 = -advantages * jnp.clip(ratio, 1 - args.clip_coef, 1 + args.clip_coef)
        pg_loss = jnp.maximum(pg_loss1, pg_loss2).mean()

        # Value loss
        v_loss = 0.5 * ((newvalue - target_values) ** 2).mean()
        entropy_loss = entropy.mean()
        loss = pg_loss - args.ent_coef * entropy_loss + v_loss * args.vf_coef
        return loss, (pg_loss, v_loss, entropy_loss, jax.lax.stop_gradient(approx_kl))

    @jax.jit
    def single_device_update(
        agent_state: TrainState,
        sharded_storages: List,
        sharded_next_obs: List,
        sharded_next_done: List,
        key: jax.random.PRNGKey,
    ):
        storage = jax.tree_map(lambda *x: jnp.hstack(x), *sharded_storages)
        next_obs = jnp.concatenate(sharded_next_obs)
        next_done = jnp.concatenate(sharded_next_done)
        ppo_loss_grad_fn = jax.value_and_grad(ppo_loss, has_aux=True)
        advantages, target_values = compute_gae(agent_state, next_obs, next_done, storage)
        if args.norm_adv: # NOTE: per-minibatch advantages normalization
            advantages = advantages.reshape(advantages.shape[0], args.num_minibatches, -1)
            advantages = (advantages - advantages.mean((0, -1), keepdims=True)) / (advantages.std((0, -1), keepdims=True) + 1e-8)
            advantages = advantages.reshape(advantages.shape[0], -1)

        def update_epoch(carry, _):
            agent_state, key = carry
            key, subkey = jax.random.split(key)

            def flatten(x):
                return x.reshape((-1,) + x.shape[2:])

            # taken from: https://github.com/google/brax/blob/main/brax/training/agents/ppo/train.py
            def convert_data(x: jnp.ndarray):
                x = jax.random.permutation(subkey, x)
                x = jnp.reshape(x, (args.num_minibatches * args.gradient_accumulation_steps, -1) + x.shape[1:])
                return x

            flatten_storage = jax.tree_map(flatten, storage)
            flatten_advantages = flatten(advantages)
            flatten_target_values = flatten(target_values)
            shuffled_storage = jax.tree_map(convert_data, flatten_storage)
            shuffled_advantages = convert_data(flatten_advantages)
            shuffled_target_values = convert_data(flatten_target_values)

            def update_minibatch(agent_state, minibatch):
                mb_obs, mb_actions, mb_behavior_logprobs, mb_firststeps, mb_advantages, mb_target_values = minibatch
                (loss, (pg_loss, v_loss, entropy_loss, approx_kl)), grads = ppo_loss_grad_fn(
                    agent_state.params,
                    mb_obs,
                    mb_actions,
                    mb_behavior_logprobs,
                    mb_firststeps,
                    mb_advantages,
                    mb_target_values,
                )
                grads = jax.lax.pmean(grads, axis_name="local_devices")
                agent_state = agent_state.apply_gradients(grads=grads)
                return agent_state, (loss, pg_loss, v_loss, entropy_loss, approx_kl)

            agent_state, (loss, pg_loss, v_loss, entropy_loss, approx_kl) = jax.lax.scan(
                update_minibatch,
                agent_state,
                (
                    shuffled_storage.obs,
                    shuffled_storage.actions,
                    shuffled_storage.logprobs,
                    shuffled_storage.firststeps,
                    shuffled_advantages,
                    shuffled_target_values,
                ),
            )
            return (agent_state, key), (loss, pg_loss, v_loss, entropy_loss, approx_kl)

        (agent_state, key), (loss, pg_loss, v_loss, entropy_loss, approx_kl) = jax.lax.scan(
            update_epoch, (agent_state, key), (), length=args.update_epochs
        )
        loss = jax.lax.pmean(loss, axis_name="local_devices").mean()
        pg_loss = jax.lax.pmean(pg_loss, axis_name="local_devices").mean()
        v_loss = jax.lax.pmean(v_loss, axis_name="local_devices").mean()
        entropy_loss = jax.lax.pmean(entropy_loss, axis_name="local_devices").mean()
        approx_kl = jax.lax.pmean(approx_kl, axis_name="local_devices").mean()
        return agent_state, loss, pg_loss, v_loss, entropy_loss, approx_kl, key

    multi_device_update = jax.pmap(
        single_device_update,
        axis_name="local_devices",
        devices=global_learner_decices,
    )

    params_queues = []
    rollout_queues = []
    dummy_writer = SimpleNamespace()
    dummy_writer.add_scalar = lambda x, y, z: None

    unreplicated_params = flax.jax_utils.unreplicate(agent_state.params)
    for d_idx, d_id in enumerate(args.actor_device_ids):
        device_params = jax.device_put(unreplicated_params, local_devices[d_id])
        for thread_id in range(args.num_actor_threads):
            params_queues.append(queue.Queue(maxsize=1))
            rollout_queues.append(queue.Queue(maxsize=1))
            params_queues[-1].put(device_params)
            threading.Thread(
                target=rollout,
                args=(
                    jax.device_put(key, local_devices[d_id]),
                    args,
                    rollout_queues[-1],
                    params_queues[-1],
                    writer if d_idx == 0 and thread_id == 0 else dummy_writer,
                    learner_devices,
                    d_idx * args.num_actor_threads + thread_id,
                    local_devices[d_id],
                ),
            ).start()

    rollout_queue_get_time = deque(maxlen=10)
    data_transfer_time = deque(maxlen=10)
    learner_policy_version = 0
    while True:
        learner_policy_version += 1
        rollout_queue_get_time_start = time.time()
        sharded_storages = []
        sharded_next_obss = []
        sharded_next_dones = []
        for d_idx, d_id in enumerate(args.actor_device_ids):
            for thread_id in range(args.num_actor_threads):
                (
                    global_step,
                    actor_policy_version,
                    update,
                    sharded_storage,
                    sharded_next_obs,
                    sharded_next_done,
                    avg_params_queue_get_time,
                    device_thread_id,
                ) = rollout_queues[d_idx * args.num_actor_threads + thread_id].get()
                sharded_storages.append(sharded_storage)
                sharded_next_obss.append(sharded_next_obs)
                sharded_next_dones.append(sharded_next_done)
        rollout_queue_get_time.append(time.time() - rollout_queue_get_time_start)
        training_time_start = time.time()
        (agent_state, loss, pg_loss, v_loss, entropy_loss, approx_kl, learner_keys) = multi_device_update(
            agent_state,
            sharded_storages,
            sharded_next_obss,
            sharded_next_dones,
            learner_keys,
        )
        unreplicated_params = flax.jax_utils.unreplicate(agent_state.params)
        for d_idx, d_id in enumerate(args.actor_device_ids):
            device_params = jax.device_put(unreplicated_params, local_devices[d_id])
            for thread_id in range(args.num_actor_threads):
                params_queues[d_idx * args.num_actor_threads + thread_id].put(device_params)

        # record rewards for plotting purposes
        if learner_policy_version % args.log_frequency == 0:
            writer.add_scalar("stats/rollout_queue_get_time", np.mean(rollout_queue_get_time), global_step)
            writer.add_scalar(
                "stats/rollout_params_queue_get_time_diff",
                np.mean(rollout_queue_get_time) - avg_params_queue_get_time,
                global_step,
            )
            writer.add_scalar("stats/training_time", time.time() - training_time_start, global_step)
            writer.add_scalar("stats/rollout_queue_size", rollout_queues[-1].qsize(), global_step)
            writer.add_scalar("stats/params_queue_size", params_queues[-1].qsize(), global_step)
            print(
                global_step,
                f"actor_policy_version={actor_policy_version}, actor_update={update}, learner_policy_version={learner_policy_version}, training time: {time.time() - training_time_start}s",
            )
            writer.add_scalar(
                "charts/learning_rate", agent_state.opt_state[2][1].hyperparams["learning_rate"][-1].item(), global_step
            )
            writer.add_scalar("losses/value_loss", v_loss[-1].item(), global_step)
            writer.add_scalar("losses/policy_loss", pg_loss[-1].item(), global_step)
            writer.add_scalar("losses/entropy", entropy_loss[-1].item(), global_step)
            writer.add_scalar("losses/approx_kl", approx_kl[-1].item(), global_step)
            writer.add_scalar("losses/loss", loss[-1].item(), global_step)
        if learner_policy_version >= args.num_updates:
            break

    envs.close()
    writer.close()
