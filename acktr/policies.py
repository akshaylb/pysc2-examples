import numpy as np
import tensorflow as tf
from baselines.acktr.utils import conv, fc, dense, conv_to_fc, sample, kl_div
from baselines.common.distributions import make_pdtype
import baselines.common.tf_util as U
from pysc2.lib.features import actions

class CnnPolicy(object):

  def __init__(self, sess, ob_space, ac_space, nenv, nsteps, nstack, reuse=False):
    nbatch = nenv*nsteps
    nh, nw, nc = ob_space
    ob_shape = (nbatch, nh, nw, nc*nstack)
    nact = 524 # StarCraft II total base actions
    nact2 = 500 # StarCraft II 2nd action param
    X = tf.placeholder(tf.uint8, ob_shape) #obs
    #AVAIL = tf.placeholder(tf.uint8, [nbatch, 524]) #one-hot encoded available actions
    # [0, 0, 1, 0, 1, 0, 0, 0, ... 1]
    with tf.variable_scope("model", reuse=reuse):
      h = conv(tf.cast(X, tf.float32), 'c1', nf=16, rf=1, stride=1, init_scale=np.sqrt(2))
      h2 = conv(h, 'c2', nf=32, rf=1, stride=1, init_scale=np.sqrt(2))
      h3 = conv_to_fc(h2)
      h4 = fc(h3, 'fc1', nh=256, init_scale=np.sqrt(2))
      pi = fc(h4, 'pi', nact, act=lambda x:x) # ( nenv * nsteps, 524)
      pi2 = fc(h4, 'pi2', nact2, act=lambda x:x) # ( nenv * nsteps, 500)
      vf = fc(h4, 'v', 1, act=lambda x:x) # ( nenv * nsteps, 1)

      # 1 x 1 convolution for dimensionality reduction
      xy1 = conv(h2, 'xy1', nf=1, rf=1, stride=1, init_scale=np.sqrt(2)) # (? nenv * nsteps, 64, 64, 1)
      x1 = xy1[:,:,0,0]
      y1 = xy1[:,0,:,0]
      xy2 = conv(h2, 'xy2', nf=1, rf=1, stride=1, init_scale=np.sqrt(2)) # (? nenv * nsteps, 64, 64, 1)
      x2 = xy2[:,:,0,0]
      y2 = xy2[:,0,:,0]

    v0 = vf[:, 0]
    #pi = pi * AVAIL # masking base action space by [available actions]
    #a0 = sample(pi)
    #total_a2 = nact2
    #AVAIL2 = np.concatenate(([1]*1,[0]*10), axis=0)
    #a2_size = actions.FUNCTIONS[a0].args[0].sizes[0]
    #pi2 = pi2 * AVAIL2 # TODO : masking 2nd action param 0 ~ 500
    #a1 = sample(pi2)
    action = [pi, pi2, x1, y1, x2, y2]

    self.initial_state = [] #not stateful

    def step(ob, *_args, **_kwargs):
      a, v = sess.run([action, v0], {X:ob})
      return a, v, [] #dummy state

    def value(ob, *_args, **_kwargs):
      return sess.run(v0, {X:ob})

    self.X = X
    self.pi = pi
    self.pi2 = pi2
    self.x1 = x1
    self.y1 = y1
    self.x2 = x2
    self.y2 = y2
    self.vf = vf
    self.step = step
    self.value = value


class GaussianMlpPolicy(object):
  def __init__(self, ob_dim, ac_dim):
    # Here we'll construct a bunch of expressions, which will be used in two places:
    # (1) When sampling actions
    # (2) When computing loss functions, for the policy update
    # Variables specific to (1) have the word "sampled" in them,
    # whereas variables specific to (2) have the word "old" in them
    ob_no = tf.placeholder(tf.float32, shape=[None, ob_dim*2], name="ob") # batch of observations
    oldac_na = tf.placeholder(tf.float32, shape=[None, ac_dim], name="ac") # batch of actions previous actions
    oldac_dist = tf.placeholder(tf.float32, shape=[None, ac_dim*2], name="oldac_dist") # batch of actions previous action distributions
    adv_n = tf.placeholder(tf.float32, shape=[None], name="adv") # advantage function estimate
    oldlogprob_n = tf.placeholder(tf.float32, shape=[None], name='oldlogprob') # log probability of previous actions
    wd_dict = {}
    h1 = tf.nn.tanh(dense(ob_no, 64, "h1", weight_init=U.normc_initializer(1.0), bias_init=0.0, weight_loss_dict=wd_dict))
    h2 = tf.nn.tanh(dense(h1, 64, "h2", weight_init=U.normc_initializer(1.0), bias_init=0.0, weight_loss_dict=wd_dict))
    mean_na = dense(h2, ac_dim, "mean", weight_init=U.normc_initializer(0.1), bias_init=0.0, weight_loss_dict=wd_dict) # Mean control output
    self.wd_dict = wd_dict
    self.logstd_1a = logstd_1a = tf.get_variable("logstd", [ac_dim], tf.float32, tf.zeros_initializer()) # Variance on outputs
    logstd_1a = tf.expand_dims(logstd_1a, 0)
    std_1a = tf.exp(logstd_1a)
    std_na = tf.tile(std_1a, [tf.shape(mean_na)[0], 1])
    ac_dist = tf.concat([tf.reshape(mean_na, [-1, ac_dim]), tf.reshape(std_na, [-1, ac_dim])], 1)
    sampled_ac_na = tf.random_normal(tf.shape(ac_dist[:,ac_dim:])) * ac_dist[:,ac_dim:] + ac_dist[:,:ac_dim] # This is the sampled action we'll perform.
    logprobsampled_n = - U.sum(tf.log(ac_dist[:,ac_dim:]), axis=1) - 0.5 * tf.log(2.0*np.pi)*ac_dim - 0.5 * U.sum(tf.square(ac_dist[:,:ac_dim] - sampled_ac_na) / (tf.square(ac_dist[:,ac_dim:])), axis=1) # Logprob of sampled action
    logprob_n = - U.sum(tf.log(ac_dist[:,ac_dim:]), axis=1) - 0.5 * tf.log(2.0*np.pi)*ac_dim - 0.5 * U.sum(tf.square(ac_dist[:,:ac_dim] - oldac_na) / (tf.square(ac_dist[:,ac_dim:])), axis=1) # Logprob of previous actions under CURRENT policy (whereas oldlogprob_n is under OLD policy)
    kl = U.mean(kl_div(oldac_dist, ac_dist, ac_dim))
    #kl = .5 * U.mean(tf.square(logprob_n - oldlogprob_n)) # Approximation of KL divergence between old policy used to generate actions, and new policy used to compute logprob_n
    surr = - U.mean(adv_n * logprob_n) # Loss function that we'll differentiate to get the policy gradient
    surr_sampled = - U.mean(logprob_n) # Sampled loss of the policy
    self._act = U.function([ob_no], [sampled_ac_na, ac_dist, logprobsampled_n]) # Generate a new action and its logprob
    #self.compute_kl = U.function([ob_no, oldac_na, oldlogprob_n], kl) # Compute (approximate) KL divergence between old policy and new policy
    self.compute_kl = U.function([ob_no, oldac_dist], kl)
    self.update_info = ((ob_no, oldac_na, adv_n), surr, surr_sampled) # Input and output variables needed for computing loss
    U.initialize() # Initialize uninitialized TF variables

  def act(self, ob):
    ac, ac_dist, logp = self._act(ob[None])
    return ac[0], ac_dist[0], logp[0]