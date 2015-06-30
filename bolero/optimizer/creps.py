# Authors: Jan Hendrik Metzen <jhm@informatik.uni-bremen.de>
#          Alexander Fabisch <afabisch@informatik.uni-bremen.de>

import numpy as np
from scipy.optimize import fmin_l_bfgs_b
from collections import deque
from ..optimizer import ContextualOptimizer
from ..utils.mathext import logsumexp
from ..utils.scaling import Scaling
from ..representation.ul_policies import (ContextTransformationPolicy,
                                          LinearGaussianPolicy,
                                          BoundedScalingPolicy)
from ..utils.validation import check_random_state
from ..utils.log import get_logger


def solve_dual_contextual_reps(s, R, epsilon, min_eta):
    """Solve dual function for C-REPS."""
    if s.shape[0] != R.shape[0]:
        raise ValueError("Number of contexts (%d) must equal number of "
                         "returns (%d)." % (s.shape[0], R.shape[0]))

    if R.ndim != 1:
        raise ValueError("Returns must be passed in a flat array!")

    # Definition of the dual function
    def g(x):  # Objective function
        eta = x[0]
        nu = x[1:]
        return (eta * epsilon + nu.T.dot(s.mean(axis=0)) +
                eta * logsumexp((R - nu.dot(s.T)) / eta, b=1.0 / len(R)))

    # Lower bound for Lagrange parameters eta and nu
    bounds = np.vstack(([[min_eta, None]], np.tile(None, (s.shape[1], 2))))
    # Start point for optimization
    x0 = [1] + [1] * s.shape[1]

    # Perform the actual optimization of the dual function
    #r = NLP(g, x0, lb=lb).solve('ralg', iprint=-10)
    r = fmin_l_bfgs_b(g, x0, approx_grad=True, bounds=bounds)
    # Fetch optimal lagrangian parameter eta. Corresponds to a temperature
    # of a softmax distribution
    eta = r[0][0]
    # Fetch optimal vale of vector nu which determines the context
    # dependent baseline
    nu = r[0][1:]

    # Determine weights of individual samples based on the their return,
    # the optimal baseline nu.dot(\phi(s)) and the "temperature" eta
    log_d = (R - nu.dot(s.T)) / eta
    # Numerically stable softmax version of the weights. Note that
    # this does neither changes the solution of the weighted least
    # squares nor the estimation of the covariance.
    d = np.exp(log_d - log_d.max())
    d /= d.sum()

    return d, r[0]


class CREPSOptimizer(ContextualOptimizer):
    """Contextual Relative Entropy Policy Search.

    Use C-REPS as a black-box contextual optimizer: Learns an upper-level
    distribution :math:`\pi(\\boldsymbol{\\theta}|\\boldsymbol{s})` which
    selects weights :math:`\\boldsymbol{\\theta}` for the objective function.
    At the moment, :math:`\pi(\\boldsymbol{\\theta}|\\boldsymbol{s})` is
    assumed to be a multivariate Gaussian distribution whose mean is a linear
    function of nonlinear features from the context. C-REPS constrains the
    learning updates such that the KL divergence between successive
    distribution is below the threshold :math:`\epsilon`.

    Parameters
    ----------
    initial_params : array-like, shape (n_params,)
        Initial parameter vector.

    variance : float, optional (default: 1.0)
        Initial exploration variance.

    covariance : array-like, optional (default: None)
        Either a diagonal (with shape (n_params,)) or a full covariance matrix
        (with shape (n_params, n_params)). A full covariance can contain
        information about the correlation of variables.

    epsilon : float, optional (default: 2.0)
        Maximum Kullback-Leibler divergence of two successive policy
        distributions.

    min_eta : float, optional (default: 1e-8)
        Minimum eta, 0 would result in numerical problems

    train_freq : int, optional (default: 25)
        Number of rollouts between policy updates.

    n_samples_per_update : int, optional (default: 100)
        Number of samples that will be used to update a policy.

    context_features : string or callable, optional (default: None)
        (Nonlinear) feature transformation for the context.

    gamma : float, optional (default: 1e-4)
        Regularization parameter. Should be removed in the future.

    bounds : array-like, shape (n_samples, 2), optional (default: None)
        Upper and lower bounds for each parameter.

    log_to_file: optional, boolean or string (default: False)
        Log results to given file, it will be located in the $BL_LOG_PATH

    log_to_stdout: optional, boolean (default: False)
        Log to standard output

    random_state : optional, int
        Seed for the random number generator.
    """
    def __init__(self, initial_params=None, variance=None, covariance=None,
                 epsilon=2.0, min_eta=1e-8, train_freq=25,
                 n_samples_per_update=100, context_features=None, gamma=1e-4,
                 bounds=None, log_to_file=False, log_to_stdout=False,
                 random_state=None, **kwargs):
        self.initial_params = initial_params
        self.variance = variance
        self.covariance = covariance
        self.epsilon = epsilon
        self.min_eta = min_eta
        self.train_freq = train_freq
        self.n_samples_per_update = n_samples_per_update
        self.context_features = context_features
        self.gamma = gamma
        self.bounds = bounds
        self.log_to_file = log_to_file
        self.log_to_stdout = log_to_stdout
        self.random_state = random_state

    def init(self, dimension, n_context_dims):
        self.logger = get_logger(self, self.log_to_file, self.log_to_stdout)

        self.random_state = check_random_state(self.random_state)

        self.it = 0

        if self.initial_params is None:
            self.initial_params = np.zeros(dimension)
        else:
            self.initial_params = np.asarray(self.initial_params).astype(
                np.float64, copy=True)
        if dimension != len(self.initial_params):
            raise ValueError("Number of dimensions (%d) does not match "
                             "number of initial parameters (%d)."
                             % (dimension, len(self.initial_params)))

        self.context = None
        self.params = None
        self.reward = None

        self.scaler = Scaling(variance=self.variance,
                              covariance=self.covariance,
                              compute_inverse=True)
        inv_scaled_params = self.scaler.inv_scale(self.initial_params)

        policy = ContextTransformationPolicy(
            LinearGaussianPolicy, dimension, n_context_dims,
            context_transformation=self.context_features,
            mean=inv_scaled_params, covariance_scale=1.0, gamma=self.gamma,
            random_state=self.random_state)
        self.policy_ = BoundedScalingPolicy(policy, self.scaler, self.bounds)

        self.history_theta = deque(maxlen=self.n_samples_per_update)
        self.history_R = deque(maxlen=self.n_samples_per_update)
        self.history_s = deque(maxlen=self.n_samples_per_update)
        self.history_phi_s = deque(maxlen=self.n_samples_per_update)

    def get_desired_context(self):
        return None

    def set_context(self, context):
        self.context = context

    def get_next_parameters(self, params, explore=True):
        """Return parameter vector that shall be evaluated next."""
        self.params = self.policy_(self.context, explore=explore)
        params[:] = self.params

    def set_evaluation_feedback(self, rewards):
        """Inform optimizer of outcome of a rollout with current weights."""
        self.reward = np.sum(rewards)
        if not np.isfinite(self.reward):
            raise ValueError("Received illegal reward. Check your environment!")

        inv_scaled_params = self.scaler.inv_scale(self.params)
        phi_s = self.policy_.transform_context(self.context)

        self.history_theta.append(inv_scaled_params)
        self.history_R.append(self.reward)
        self.history_s.append(self.context)
        self.history_phi_s.append(phi_s)

        self.it += 1

        if self.it % self.train_freq == 0:
            self._update()

        self.logger.info("Reward %.6f" % self.reward)

    def _update(self):
        phi_s = np.asarray(self.history_phi_s)
        theta = np.asarray(self.history_theta)
        R = np.asarray(self.history_R)

        d, _ = solve_dual_contextual_reps(phi_s, R, self.epsilon, self.min_eta)
        # NOTE the context have already been transformed
        self.policy_.fit(phi_s, theta, d, context_transform=False)

    def best_policy(self):
        return self.policy_

    def is_behavior_learning_done(self):
        return False

    def __getstate__(self):
        d = dict(self.__dict__)
        del d["logger"]
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)
        self.logger = get_logger(self, self.log_to_file, self.log_to_stdout)