# coding=utf-8
# Copyright 2020 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Policy for reward prediction and boltzmann exploration."""

from __future__ import absolute_import
from __future__ import division
# Using Type Annotations.
from __future__ import print_function

from typing import Optional, Text, Tuple, Sequence

import gin
import tensorflow as tf  # pylint: disable=g-explicit-tensorflow-version-import
import tensorflow_probability as tfp

from tf_agents.bandits.networks import heteroscedastic_q_network
from tf_agents.bandits.policies import constraints as constr
from tf_agents.bandits.specs import utils as bandit_spec_utils
from tf_agents.distributions import shifted_categorical
from tf_agents.policies import tf_policy
from tf_agents.policies import utils as policy_utilities
from tf_agents.specs import tensor_spec
from tf_agents.trajectories import policy_step
from tf_agents.typing import types


@gin.configurable
class BoltzmannRewardPredictionPolicy(tf_policy.TFPolicy):
  """Class to build Reward Prediction Policies with Boltzmann exploration."""

  def __init__(self,
               time_step_spec: types.TimeStep,
               action_spec: types.NestedTensorSpec,
               reward_network: types.Network,
               temperature: types.FloatOrReturningFloat = 1.0,
               boltzmann_gumbel_exploration_constant: Optional[
                   types.Float] = None,
               observation_and_action_constraint_splitter: Optional[
                   types.Splitter] = None,
               accepts_per_arm_features: bool = False,
               constraints: Tuple[constr.NeuralConstraint, ...] = (),
               emit_policy_info: Tuple[Text, ...] = (),
               num_samples_list: Sequence[tf.Variable] = (),
               name: Optional[Text] = None):
    """Builds a BoltzmannRewardPredictionPolicy given a reward network.

    This policy takes a tf_agents.Network predicting rewards and chooses an
    action with weighted probabilities (i.e., using a softmax over the network
    estimates of value for each action).

    Args:
      time_step_spec: A `TimeStep` spec of the expected time_steps.
      action_spec: A nest of BoundedTensorSpec representing the actions.
      reward_network: An instance of a `tf_agents.network.Network`,
        callable via `network(observation, step_type) -> (output, final_state)`.
      temperature: float or callable that returns a float. The temperature used
        in the Boltzmann exploration.
      boltzmann_gumbel_exploration_constant: optional positive float. When
        provided, the policy implements Neural Bandit with Boltzmann-Gumbel
        exploration from the paper:
        N. Cesa-Bianchi et al., "Boltzmann Exploration Done Right", NIPS 2017.
      observation_and_action_constraint_splitter: A function used for masking
        valid/invalid actions with each state of the environment. The function
        takes in a full observation and returns a tuple consisting of 1) the
        part of the observation intended as input to the network and 2) the
        mask.  The mask should be a 0-1 `Tensor` of shape
        `[batch_size, num_actions]`. This function should also work with a
        `TensorSpec` as input, and should output `TensorSpec` objects for the
        observation and mask.
      accepts_per_arm_features: (bool) Whether the policy accepts per-arm
        features.
      constraints: iterable of constraints objects that are instances of
        `tf_agents.bandits.agents.NeuralConstraint`.
      emit_policy_info: (tuple of strings) what side information we want to get
        as part of the policy info. Allowed values can be found in
        `policy_utilities.PolicyInfo`.
      num_samples_list: list or tuple of tf.Variable's. Used only in
        Boltzmann-Gumbel exploration. Otherwise, empty.
      name: The name of this policy. All variables in this module will fall
        under that name. Defaults to the class name.

    Raises:
      NotImplementedError: If `action_spec` contains more than one
        `BoundedTensorSpec` or the `BoundedTensorSpec` is not valid.
    """
    policy_utilities.check_no_mask_with_arm_features(
        accepts_per_arm_features, observation_and_action_constraint_splitter)
    flat_action_spec = tf.nest.flatten(action_spec)
    if len(flat_action_spec) > 1:
      raise NotImplementedError(
          'action_spec can only contain a single BoundedTensorSpec.')

    self._temperature = temperature
    action_spec = flat_action_spec[0]
    if (not tensor_spec.is_bounded(action_spec) or
        not tensor_spec.is_discrete(action_spec) or
        action_spec.shape.rank > 1 or
        action_spec.shape.num_elements() != 1):
      raise NotImplementedError(
          f'action_spec must be a BoundedTensorSpec of type int32 and shape (). Found {action_spec}.'
      )
    self._expected_num_actions = action_spec.maximum - action_spec.minimum + 1
    self._action_offset = action_spec.minimum
    reward_network.create_variables()
    self._reward_network = reward_network
    self._constraints = constraints

    self._boltzmann_gumbel_exploration_constant = (
        boltzmann_gumbel_exploration_constant)
    self._num_samples_list = num_samples_list
    if self._boltzmann_gumbel_exploration_constant is not None:
      if self._boltzmann_gumbel_exploration_constant <= 0.0:
        raise ValueError(
            'The Boltzmann-Gumbel exploration constant is expected to be ',
            'positive. Found: ', self._boltzmann_gumbel_exploration_constant)
      if self._action_offset > 0:
        raise NotImplementedError('Action offset is not supported when ',
                                  'Boltzmann-Gumbel exploration is enabled.')
      if accepts_per_arm_features:
        raise NotImplementedError(
            'Boltzmann-Gumbel exploration is not supported ',
            'for arm features case.')
      if len(self._num_samples_list) != self._expected_num_actions:
        raise ValueError(
            'Size of num_samples_list: ', len(self._num_samples_list),
            ' does not match the expected number of actions:',
            self._expected_num_actions)

    self._emit_policy_info = emit_policy_info
    predicted_rewards_mean = ()
    if policy_utilities.InfoFields.PREDICTED_REWARDS_MEAN in emit_policy_info:
      predicted_rewards_mean = tensor_spec.TensorSpec(
          [self._expected_num_actions])
    bandit_policy_type = ()
    if policy_utilities.InfoFields.BANDIT_POLICY_TYPE in emit_policy_info:
      bandit_policy_type = (
          policy_utilities.create_bandit_policy_type_tensor_spec(shape=[1]))
    if accepts_per_arm_features:
      # The features for the chosen arm is saved to policy_info.
      chosen_arm_features_info = (
          policy_utilities.create_chosen_arm_features_info_spec(
              time_step_spec.observation))
      info_spec = policy_utilities.PerArmPolicyInfo(
          predicted_rewards_mean=predicted_rewards_mean,
          bandit_policy_type=bandit_policy_type,
          chosen_arm_features=chosen_arm_features_info)
    else:
      info_spec = policy_utilities.PolicyInfo(
          predicted_rewards_mean=predicted_rewards_mean,
          bandit_policy_type=bandit_policy_type)

    self._accepts_per_arm_features = accepts_per_arm_features

    super(BoltzmannRewardPredictionPolicy, self).__init__(
        time_step_spec, action_spec,
        policy_state_spec=reward_network.state_spec,
        clip=False,
        info_spec=info_spec,
        emit_log_probability='log_probability' in emit_policy_info,
        observation_and_action_constraint_splitter=(
            observation_and_action_constraint_splitter),
        name=name)

  @property
  def accepts_per_arm_features(self):
    return self._accepts_per_arm_features

  def _variables(self):
    policy_variables = self._reward_network.variables
    for c in self._constraints:
      policy_variables.append(c.variables)
    return policy_variables

  def _get_temperature_value(self):
    if callable(self._temperature):
      return self._temperature()
    return self._temperature

  def _distribution(self, time_step, policy_state):
    observation = time_step.observation
    if self.observation_and_action_constraint_splitter is not None:
      observation, _ = self.observation_and_action_constraint_splitter(
          observation)

    predictions, policy_state = self._reward_network(
        observation, time_step.step_type, policy_state)
    batch_size = tf.shape(predictions)[0]

    if isinstance(self._reward_network,
                  heteroscedastic_q_network.HeteroscedasticQNetwork):
      predicted_reward_values = predictions.q_value_logits
    else:
      predicted_reward_values = predictions

    predicted_reward_values.shape.with_rank_at_least(2)
    predicted_reward_values.shape.with_rank_at_most(3)
    if predicted_reward_values.shape[
        -1] is not None and predicted_reward_values.shape[
            -1] != self._expected_num_actions:
      raise ValueError(
          'The number of actions ({}) does not match the reward_network output'
          ' size ({}).'.format(self._expected_num_actions,
                               predicted_reward_values.shape[1]))

    mask = constr.construct_mask_from_multiple_sources(
        time_step.observation, self._observation_and_action_constraint_splitter,
        self._constraints, self._expected_num_actions)

    if self._boltzmann_gumbel_exploration_constant is not None:
      logits = predicted_reward_values

      # Apply masking if needed. Overwrite the logits for invalid actions to
      # logits.dtype.min.
      if mask is not None:
        almost_neg_inf = tf.constant(logits.dtype.min, dtype=logits.dtype)
        logits = tf.compat.v2.where(
            tf.cast(mask, tf.bool), logits, almost_neg_inf)

      gumbel_dist = tfp.distributions.Gumbel(loc=0., scale=1.)
      gumbel_samples = gumbel_dist.sample(tf.shape(logits))
      num_samples_list_float = tf.stack(
          [tf.cast(x.read_value(), tf.float32) for x in self._num_samples_list],
          axis=-1)
      exploration_weights = tf.math.divide_no_nan(
          self._boltzmann_gumbel_exploration_constant,
          tf.sqrt(num_samples_list_float))
      final_logits = logits + exploration_weights * gumbel_samples
      actions = tf.cast(
          tf.math.argmax(final_logits, axis=1), self._action_spec.dtype)
      # Log probability is not available in closed form. We treat this as a
      # deterministic policy at the moment.
      log_probability = tf.zeros([batch_size], tf.float32)
    else:
      # Apply the temperature scaling, needed for Boltzmann exploration.
      logits = predicted_reward_values / self._get_temperature_value()

      # Apply masking if needed. Overwrite the logits for invalid actions to
      # logits.dtype.min.
      if mask is not None:
        almost_neg_inf = tf.constant(logits.dtype.min, dtype=logits.dtype)
        logits = tf.compat.v2.where(
            tf.cast(mask, tf.bool), logits, almost_neg_inf)

      if self._action_offset != 0:
        distribution = shifted_categorical.ShiftedCategorical(
            logits=logits,
            dtype=self._action_spec.dtype,
            shift=self._action_offset)
      else:
        distribution = tfp.distributions.Categorical(
            logits=logits,
            dtype=self._action_spec.dtype)

      actions = distribution.sample()
      log_probability = distribution.log_prob(actions)

    bandit_policy_values = tf.fill([batch_size, 1],
                                   policy_utilities.BanditPolicyType.BOLTZMANN)

    if self._accepts_per_arm_features:
      # Saving the features for the chosen action to the policy_info.
      def gather_observation(obs):
        return tf.gather(params=obs, indices=actions, batch_dims=1)

      chosen_arm_features = tf.nest.map_structure(
          gather_observation,
          observation[bandit_spec_utils.PER_ARM_FEATURE_KEY])
      policy_info = policy_utilities.PerArmPolicyInfo(
          log_probability=log_probability if
          policy_utilities.InfoFields.LOG_PROBABILITY in self._emit_policy_info
          else (),
          predicted_rewards_mean=(
              predicted_reward_values if policy_utilities.InfoFields
              .PREDICTED_REWARDS_MEAN in self._emit_policy_info else ()),
          bandit_policy_type=(bandit_policy_values
                              if policy_utilities.InfoFields.BANDIT_POLICY_TYPE
                              in self._emit_policy_info else ()),
          chosen_arm_features=chosen_arm_features)
    else:
      policy_info = policy_utilities.PolicyInfo(
          log_probability=log_probability if
          policy_utilities.InfoFields.LOG_PROBABILITY in self._emit_policy_info
          else (),
          predicted_rewards_mean=(
              predicted_reward_values if policy_utilities.InfoFields
              .PREDICTED_REWARDS_MEAN in self._emit_policy_info else ()),
          bandit_policy_type=(bandit_policy_values
                              if policy_utilities.InfoFields.BANDIT_POLICY_TYPE
                              in self._emit_policy_info else ()))

    return policy_step.PolicyStep(
        tfp.distributions.Deterministic(loc=actions), policy_state, policy_info)
