import collections

import tensorflow as tf
import numpy as np
import math as m

from Neural_Networks import vae_utils

# based on implementation here:
# https://github.com/tensorflow/models/blob/master/autoencoder/autoencoder_models/VariationalAutoencoder.py

SMALL_CONSTANT = 1e-6

class VariationalAutoencoder(object):

    def __init__(self, name, n_input, n_hidden, n_weights, middle="gaussian"):
        
        self.n_input = n_input
        self.n_hidden = n_hidden
        self.n_weights = n_weights
        self.name = name
        self.middle = middle
        self.bias_start = 0.0

        network_weights = self._create_weights()
        self.weights = network_weights

        self.nonlinearity = tf.nn.relu
        self.nonlinearity2 = tf.nn.leaky_relu


    def _calc_z_mean_and_sigma(self,x):
        with tf.name_scope("VICI_VAE_encoder"):
            hidden1_pre = tf.add(tf.matmul(x, self.weights['VICI_VAE_encoder']['W3_to_hidden']), self.weights['VICI_VAE_encoder']['b3_to_hidden'])
            hidden1_post = self.nonlinearity(hidden1_pre)
#            hidden1_post = tf.nn.batch_normalization(hidden1_post,tf.Variable(tf.zeros([400], dtype=tf.float32)),tf.Variable(tf.ones([400], dtype=tf.float32)),None,None,0.000001,name="e_b_norm_1")

#            hidden3_pre = tf.add(tf.matmul(hidden1_post, self.weights['VAE_encoder']['W3b_hth']), self.weights['VAE_encoder']['b3b_hth'])
#            hidden3_post = self.nonlinearity(hidden3_pre)
##            
#            hidden4_pre = tf.add(tf.matmul(hidden3_post, self.weights['VAE_encoder']['W3c_hth']), self.weights['VAE_encoder']['b3c_hth'])
#            hidden4_post = self.nonlinearity(hidden4_pre)
#            
#            hidden5_pre = tf.add(tf.matmul(hidden4_post, self.weights['encoder']['W3d_hth']), self.weights['encoder']['b3d_hth'])
#            hidden5_post = self.nonlinearity(hidden5_pre)
            
#            hidden2_pre = tf.add(tf.matmul(hidden1_post, self.weights['encoder']['W3_hth']), self.weights['encoder']['b3_hth'])
#            hidden2_post = self.nonlinearity(hidden2_pre)
#            hidden2_post = hidden1_post

            z_mean = tf.add(tf.matmul(hidden1_post, self.weights['VICI_VAE_encoder']['W4_to_mu']), self.weights['VICI_VAE_encoder']['b4_to_mu'])
#            z_mean = self.nonlinearity2(z_mean)
#            z_mean = tf.exp(z_mean)
            z_log_sigma_sq = tf.add(tf.matmul(hidden1_post, self.weights['VICI_VAE_encoder']['W5_to_log_sigma']), self.weights['VICI_VAE_encoder']['b5_to_log_sigma'])
#            z_log_sigma_sq = self.nonlinearity(z_log_sigma_sq+10)-10
            tf.summary.histogram("z_mean", z_mean)
            tf.summary.histogram("z_log_sigma_sq", z_log_sigma_sq)
            return z_mean, z_log_sigma_sq

    def _sample_from_gaussian_dist(self, num_rows, num_cols, mean, log_sigma_sq):
        with tf.name_scope("sample_in_z_space"):
            eps = tf.random_normal([num_rows, num_cols], 0, 1., dtype=tf.float32)
            sample = tf.add(mean, tf.multiply(tf.sqrt(tf.exp(log_sigma_sq)), eps))
        return sample

    def _create_weights(self):
        all_weights = collections.OrderedDict()
        with tf.variable_scope("VICI_VAE_ENC"):
            # Encoder
            all_weights['VICI_VAE_encoder'] = collections.OrderedDict()
            hidden_number_encoder = self.n_weights
            all_weights['VICI_VAE_encoder']['W3_to_hidden'] = tf.Variable(vae_utils.xavier_init(self.n_input, hidden_number_encoder), dtype=tf.float32)
            tf.summary.histogram("W3_to_hidden", all_weights['VICI_VAE_encoder']['W3_to_hidden'])
    
    #        all_weights['encoder']['W3_hth'] = tf.Variable(vae_utils.xavier_init(hidden_number_encoder, hidden_number_encoder), dtype=tf.float32)
    #        tf.summary.histogram("W3_hth", all_weights['encoder']['W3_hth'])
    #        
#            all_weights['VAE_encoder']['W3b_hth'] = tf.Variable(vae_utils.xavier_init(hidden_number_encoder, hidden_number_encoder), dtype=tf.float32)
#            tf.summary.histogram("W3b_hth", all_weights['VAE_encoder']['W3b_hth'])
##    #        
#            all_weights['VAE_encoder']['W3c_hth'] = tf.Variable(vae_utils.xavier_init(hidden_number_encoder, hidden_number_encoder), dtype=tf.float32)
#            tf.summary.histogram("W3c_hth", all_weights['VAE_encoder']['W3c_hth'])
    #        
    #        all_weights['encoder']['W3d_hth'] = tf.Variable(vae_utils.xavier_init(hidden_number_encoder, hidden_number_encoder), dtype=tf.float32)
    #        tf.summary.histogram("W3d_hth", all_weights['encoder']['W3d_hth'])
    
            all_weights['VICI_VAE_encoder']['W4_to_mu'] = tf.Variable(vae_utils.xavier_init(hidden_number_encoder, self.n_hidden),dtype=tf.float32)
            tf.summary.histogram("W4_to_mu", all_weights['VICI_VAE_encoder']['W4_to_mu'])
    
            all_weights['VICI_VAE_encoder']['W5_to_log_sigma'] = tf.Variable(vae_utils.xavier_init(hidden_number_encoder, self.n_hidden), dtype=tf.float32)
            tf.summary.histogram("W5_to_log_sigma", all_weights['VICI_VAE_encoder']['W5_to_log_sigma'])
    
            all_weights['VICI_VAE_encoder']['b3_to_hidden'] = tf.Variable(tf.zeros([hidden_number_encoder], dtype=tf.float32) * self.bias_start)
            all_weights['VICI_VAE_encoder']['b3_hth'] = tf.Variable(tf.zeros([hidden_number_encoder], dtype=tf.float32) * self.bias_start)
            all_weights['VICI_VAE_encoder']['b3b_hth'] = tf.Variable(tf.zeros([hidden_number_encoder], dtype=tf.float32) * self.bias_start)
            all_weights['VICI_VAE_encoder']['b3c_hth'] = tf.Variable(tf.zeros([hidden_number_encoder], dtype=tf.float32) * self.bias_start)
            all_weights['VICI_VAE_encoder']['b3d_hth'] = tf.Variable(tf.zeros([hidden_number_encoder], dtype=tf.float32) * self.bias_start)
            all_weights['VICI_VAE_encoder']['b4_to_mu'] = tf.Variable(tf.zeros([self.n_hidden], dtype=tf.float32) * self.bias_start, dtype=tf.float32)
            all_weights['VICI_VAE_encoder']['b5_to_log_sigma'] = tf.Variable(tf.zeros([self.n_hidden], dtype=tf.float32) * self.bias_start, dtype=tf.float32)
            
            all_weights['prior_param'] = collections.OrderedDict()
        
        return all_weights