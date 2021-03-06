import tensorflow as tf
from tensorflow.examples.tutorials.mnist import input_data
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import os
import sys
import csv
import utils_csv
import utils_tf as utils
from cleverhans.utils_tf import model_train, model_eval
from cleverhans.attacks import FastGradientMethod
from cleverhans.model import Model
print("Tensorflow version " + tf.__version__)

config_num = int(sys.argv[1]) if len(sys.argv) > 1 else 1  # Choose type of learning technique according to config_dict
config_dict = {0: "backprop", 1: "biprop", 2: "halfbiprop", 3: "nobias_backprop", 4: "nobias_biprop", 5: "nobias_halfbiprop"}

model_name = sys.argv[0].replace(".py", "") + "_" + config_dict[config_num]
print("Model name: " + model_name)

# for reproducibility
np.random.seed(0)
tf.set_random_seed(0)

# Download images and labels into mnist.test (10K images+labels) and mnist.train (60K images+labels)
mnist = input_data.read_data_sets("data/mnist", one_hot=True, reshape=False, validation_size=0)

sess = tf.InteractiveSession()

# two convolutional layers with their channel counts, and
# one fully connected layer
K = 64  # first convolutional layer output depth
L = 128  # second convolutional layer output depth
M = 1024  # fully connected

Z_dim = 10 # Random layer

with tf.name_scope("input"):
    # input X: 28x28 grayscale images, the first dimension (None) will index the images in the mini-batch
    X = tf.placeholder(tf.float32, [None, 28, 28, 1])
    X_noisy = tf.placeholder(tf.float32, [None, 28, 28, 1])
    X_adv = tf.placeholder(tf.float32, [None, 28, 28, 1])

    # output Y_: labels for classification and generation
    Y_ = tf.placeholder(tf.float32, [None, 10])

    # variable batch size
    BS = tf.placeholder(tf.int32)

    train_phase = tf.placeholder(tf.bool)

    # random input for Generator
    Z = tf.placeholder(tf.float32, shape=[None, Z_dim])

    input_test_sum = tf.summary.image("input", X, 10)
    input_noisy_sum = tf.summary.image("input-noisy", X_noisy, 10)
    input_adv_sum = tf.summary.image("input-adv", X_adv, 10)

def sample_Z(m, n):
    return np.random.uniform(-1., 1., size=[m, n])

# From tensorflow-generative-model-collections
def lrelu(x, leak=0.2):
    return tf.maximum(x, leak*x)

with tf.name_scope("classifier-generator"):
    # Weights for classifier and generator
    C_W1 = utils.weight_variable([4, 4, 1, K], name="C_W1")
    C_W2 = utils.weight_variable([4, 4, K, L], name="C_W2")

    C_W3 = utils.weight_variable([7 * 7 * L, M], name="C_W3")
    C_W4 = utils.weight_variable([M, Z_dim], name="C_W4")

def classifier(x, is_training=True, reuse=None):
    with tf.variable_scope("classifier", reuse=reuse) as scope_c:
        # Variables for classifier
        C_B1 = utils.bias_variable([K], name="C_B1")
        C_B2 = utils.bias_variable([L], name="C_B2")
        C_B3 = utils.bias_variable([M], name="C_B3")
        C_B4 = utils.bias_variable([Z_dim], name="C_B4")

        stride = 2  # output is 14x14
        H1 = lrelu(tf.nn.conv2d(x, C_W1, strides=[1, stride, stride, 1], padding='SAME') + C_B1)
        stride = 2  # output is 7x7
        H2 = lrelu(utils.bn((tf.nn.conv2d(H1, C_W2, strides=[1, stride, stride, 1], padding='SAME') + C_B2), is_training=is_training,scope="C_bn_h2"))

        # reshape the output from the third convolution for the fully connected layer
        HH2 = tf.reshape(H2, shape=[-1, 7 * 7 * L])

        H3 = tf.nn.relu(tf.matmul(HH2, C_W3) + C_B3)
        Ylogits = tf.matmul(H3, C_W4) + C_B4

        Ysigmoid = tf.nn.sigmoid(Ylogits)
        Ysoftmax = tf.nn.softmax(Ylogits)

        return Ysoftmax, Ysigmoid, Ylogits


class ClassifierModel(Model):
    def get_logits(self, x):
        Ysoftmax, Ysigmoid, Ylogits = classifier(x, is_training=False, reuse=True)
        return Ylogits

# Generator of random input reuses weights of classifier
def generator(z, bs, is_training=True, reuse=None):
    with tf.variable_scope("generator", reuse=reuse) as scope_g:
        # Variables for classifier
        G_B1 = utils.bias_variable([1], name="G_B1")
        G_B2 = utils.bias_variable([K], name="G_B2")
        G_B3 = utils.bias_variable([7 * 7 * L], name="G_B3")
        G_B4 = utils.bias_variable([M], name="G_B4")

        GH3 = tf.nn.relu(utils.bn((tf.matmul(z, tf.transpose(C_W4)) + G_B4), is_training=is_training,scope="G_bn_gh3"))
        GH2 = tf.nn.relu(utils.bn((tf.matmul(GH3, tf.transpose(C_W3)) + G_B3), is_training=is_training,scope="G_bn_gh2"))
        GHH2 = tf.reshape(GH2, shape=[-1, 7, 7, L])
        stride = 2  # output is 14x14
        GH1 = tf.nn.relu(tf.nn.conv2d_transpose(GHH2, C_W2, output_shape=[bs, 14, 14, K], strides=[1, stride, stride, 1]) + G_B2)#deconv2 W2
        stride = 2  # output is 28x28
        GXlogits = tf.nn.conv2d_transpose(GH1, C_W1, output_shape=[bs, 28, 28, 1], strides=[1, stride, stride, 1]) + G_B1#deconv2 W1
        GXsigmoid = tf.nn.sigmoid(GXlogits)

        return GXsigmoid, GXlogits

def discriminator(x, is_training=True, reuse=None):
    with tf.variable_scope("discriminator", reuse=reuse) as scope:
        # Variables for classifier
        D_W1 = utils.weight_variable([4, 4, 1, K], name="D_W1")
        D_B1 = utils.bias_variable([K], name="D_B1")
        D_W2 = utils.weight_variable([4, 4, K, L], name="D_W2")
        D_B2 = utils.bias_variable([L], name="D_B2")

        D_W3 = utils.weight_variable([7 * 7 * L, M], name="D_W3")
        D_B3 = utils.bias_variable([M], name="D_B3")
        D_W4 = utils.weight_variable([M, 1], name="D_W4")
        D_B4 = utils.bias_variable([1], name="D_B4")

        stride = 2  # output is 14x14
        H1 = lrelu(tf.nn.conv2d(x, D_W1, strides=[1, stride, stride, 1], padding='SAME') + D_B1)
        print(H1.shape)
        stride = 2  # output is 7x7
        H2 = lrelu(utils.bn((tf.nn.conv2d(H1, D_W2, strides=[1, stride, stride, 1], padding='SAME') + D_B2), is_training=is_training, scope="D_bn_h2"))
        print(H2.shape)

        # reshape the output from the third convolution for the fully connected layer
        HH2 = tf.reshape(H2, shape=[-1, 7 * 7 * L])

        H3 = lrelu(tf.matmul(HH2, D_W3) + D_B3)
        Ylogits = tf.matmul(H3, D_W4) + D_B4

        Ysigmoid = tf.nn.sigmoid(Ylogits)
        Ysoftmax = tf.nn.softmax(Ylogits)

        return Ysoftmax, Ysigmoid, Ylogits

def plot_generator(samples, figsize=[5,5]):
    fig = plt.figure(figsize=(figsize[0], figsize[1]))
    gs = gridspec.GridSpec(figsize[1], figsize[0])
    gs.update(wspace=0.05, hspace=0.05)
    for i, sample in enumerate(samples):
        ax = plt.subplot(gs[i])
        plt.axis('off')
        ax.set_xticklabels([])
        ax.set_yticklabels([])
        ax.set_aspect('equal')
        plt.imshow(sample.reshape((28,28)), cmap='gray')

    return fig

GXsigmoid, GXlogits = generator(Z, BS)
GXsigmoid_test, GXlogits_test = generator(Z, BS, is_training=False, reuse=True)

Ysoftmax, Ysigmoid, Ylogits = classifier(X)
model_classifier = ClassifierModel()

Ysoftmax_noisy, Ysigmoid_noisy, Ylogits_noisy = classifier(X_noisy, is_training=False, reuse=True)
Ysoftmax_adv, Ysigmoid_adv, Ylogits_adv = classifier(X_adv, is_training=False, reuse=True)

Ysoftmax_real, Ysigmoid_real, Ylogits_real = discriminator(X)
Ysoftmax_fake, Ysigmoid_fake, Ylogits_fake = discriminator(GXsigmoid, reuse=True)

with tf.name_scope("loss"):
    c_loss = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(logits=Ylogits, labels=Y_))

    d_loss_real = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=Ylogits_real, labels=tf.ones_like(Ylogits_real)))
    d_loss_fake = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=Ylogits_fake, labels=tf.zeros_like(Ylogits_fake)))
    d_loss = d_loss_real + d_loss_fake

    g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=Ylogits_fake, labels=tf.ones_like(Ylogits_fake)))

    """ Summary """
    d_loss_real_sum = tf.summary.scalar("d_loss_real", d_loss_real)
    d_loss_fake_sum = tf.summary.scalar("d_loss_fake", d_loss_fake)
    d_loss_sum = tf.summary.scalar("d_loss", d_loss)
    g_loss_sum = tf.summary.scalar("g_loss", g_loss)
    c_loss_sum = tf.summary.scalar("c_loss", c_loss)

# accuracy of the trained model, between 0 (worst) and 1 (best)
with tf.name_scope("accuracy"):
    with tf.name_scope("correct_prediction"):
        correct_prediction = tf.equal(tf.argmax(Ysoftmax, 1), tf.argmax(Y_, 1))
    with tf.name_scope("accuracy"):
        accuracy = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
    with tf.name_scope("correct_prediction_noisy"):
        correct_prediction_noisy = tf.equal(tf.argmax(Ysoftmax_noisy, 1), tf.argmax(Y_, 1))
    with tf.name_scope("accuracy_noisy"):
        accuracy_noisy = tf.reduce_mean(tf.cast(correct_prediction_noisy, tf.float32))
    with tf.name_scope("correct_prediction_adv"):
        correct_prediction_adv = tf.equal(tf.argmax(Ysoftmax_adv, 1), tf.argmax(Y_, 1))
    with tf.name_scope("accuracy_adv"):
        accuracy_adv = tf.reduce_mean(tf.cast(correct_prediction_adv, tf.float32))

    """ Summary """
    accuracy_sum = tf.summary.scalar("accuracy", accuracy)
    accuracy_noisy_sum = tf.summary.scalar("accuracy_noisy", accuracy_noisy)
    accuracy_adv_sum = tf.summary.scalar("accuracy_adv", accuracy_adv)

with tf.name_scope("max_output"):
    with tf.name_scope("max_output_test"):
        max_output_sigmoid_test = tf.reduce_max(Ysigmoid)
        max_output_softmax_test = tf.reduce_max(Ysoftmax)
    with tf.name_scope("max_output_noise"):
        max_output_sigmoid_noise = tf.reduce_max(Ysigmoid_noisy)
        max_output_softmax_noise = tf.reduce_max(Ysoftmax_noisy)
    with tf.name_scope("max_output_adv"):
        max_output_sigmoid_adv = tf.reduce_max(Ysigmoid_adv)
        max_output_softmax_adv = tf.reduce_max(Ysoftmax_adv)

    """ Summary """
    max_output_sigmoid_test_sum = tf.summary.scalar("max_output_sigmoid_test", max_output_sigmoid_test)
    max_output_softmax_test_sum = tf.summary.scalar("max_output_softmax_test", max_output_softmax_test)
    max_output_sigmoid_noise_sum = tf.summary.scalar("max_output_sigmoid_noise", max_output_sigmoid_noise)
    max_output_softmax_noise_sum = tf.summary.scalar("max_output_softmax_noise", max_output_softmax_noise)
    max_output_sigmoid_adv_sum = tf.summary.scalar("max_output_sigmoid_adv", max_output_sigmoid_adv)
    max_output_softmax_adv_sum = tf.summary.scalar("max_output_softmax_adv", max_output_softmax_adv)

utils.show_all_variables()
t_vars = tf.trainable_variables()
d_vars = [var for var in t_vars if 'D_' in var.name]
c_vars = [var for var in t_vars if 'C_' in var.name]\
    if config_num < 3 else [var for var in t_vars if 'C_W' in var.name]
g_vars = [var for var in t_vars if 'C_W' in var.name or 'G_' in var.name]\
    if config_num < 3 else c_vars

# training step
learning_rate_dis = 0.0002
learning_rate_gen = 0.001
beta1 = 0.5

with tf.name_scope("train"):
    c_train = tf.train.AdamOptimizer(learning_rate_dis, beta1=beta1).minimize(c_loss, var_list=c_vars)
    d_train = tf.train.AdamOptimizer(learning_rate_dis, beta1=beta1).minimize(d_loss, var_list=d_vars)
    g_train = tf.train.AdamOptimizer(learning_rate_gen, beta1=beta1).minimize(g_loss, var_list=g_vars)

# final summary operations
g_sum = tf.summary.merge([d_loss_fake_sum, g_loss_sum])
d_sum = tf.summary.merge([d_loss_real_sum, d_loss_sum])
c_sum = tf.summary.merge([input_test_sum, accuracy_sum, c_loss_sum, max_output_sigmoid_test_sum, max_output_softmax_test_sum])
noise_sum = tf.summary.merge([max_output_sigmoid_noise_sum, max_output_softmax_noise_sum])
noisy_sum = tf.summary.merge([input_noisy_sum, accuracy_noisy_sum])
adv_sum = tf.summary.merge([input_adv_sum, accuracy_adv_sum, max_output_sigmoid_adv_sum, max_output_softmax_adv_sum])

folder_out = 'out/' + model_name + '/'
if not os.path.exists(folder_out):
    os.makedirs(folder_out)

folder_csv = 'csv/' + model_name + '/'
if not os.path.exists(folder_csv):
    os.makedirs(folder_csv)

folder_logs = 'logs/' + model_name
if not os.path.exists(folder_csv):
    os.makedirs(folder_logs)

writer = tf.summary.FileWriter(folder_logs, sess.graph)

batch_size = 100
num_train_images = mnist.train.images.shape[0]
num_batches =  num_train_images // batch_size
all_classes = np.eye(10)

counter = 0

fgsm_params = {'eps': 0.3,
               'clip_min': 0.,
               'clip_max': 1.}

random_noise = np.random.random_sample(mnist.test.images.shape)
test_image_with_noise = np.clip(mnist.test.images + 0.1*random_noise, 0., 1.)

sample_Z_test = np.concatenate((all_classes, sample_Z(15, Z_dim)))

accuracy_list = []
sigmoid_list = []
softmax_list = []

# initialize all variables
tf.global_variables_initializer().run()

for i in range(500001):
    batch_X, batch_Y = mnist.train.next_batch(batch_size)

    if i % 5000 == 0 or i == 500000:
        counter += 1
        # Saves generated images
        samples = sess.run(GXsigmoid_test, feed_dict={Z: sample_Z(25, Z_dim), BS: 25})
        fig = plot_generator(samples)
        plt.savefig(folder_out+"gen_"+str(i).zfill(6)+'.png', bbox_inches='tight')
        plt.close(fig)

        attack_fgsm = FastGradientMethod(model_classifier, sess=sess)
        adv_x_np = attack_fgsm.generate_np(mnist.test.images, **fgsm_params)
        fig = plot_generator(adv_x_np[:25])
        plt.savefig(folder_out+"adv_"+str(i).zfill(6)+'.png', bbox_inches='tight')
        plt.close(fig)

        accu_test, c_loss_test, sigmoid_test, softmax_test, sum_c = sess.run([accuracy, c_loss, max_output_sigmoid_test, max_output_softmax_test, c_sum], {X: mnist.test.images, Y_: mnist.test.labels})
        writer.add_summary(sum_c, i)
        d_loss_test, sum_d = sess.run([d_loss, d_sum], {X: batch_X, Z: sample_Z(batch_size, Z_dim), BS: batch_size})
        writer.add_summary(sum_d, i)
        g_loss_test, sum_g = sess.run([g_loss, g_sum], {Z: sample_Z(batch_size, Z_dim), BS: batch_size})
        writer.add_summary(sum_g, i)

        print(str(i) + ": epoch " + str(i*batch_size//mnist.train.images.shape[0]+1)\
            + " - test loss class: " + str(c_loss_test) + " test loss gen: " + str(g_loss_test) + " test loss dis: " + str(d_loss_test))
        print("Real test images     - Sigmoid: " + str(sigmoid_test) + "\tSoftmax: " + str(softmax_test) + "\taccuracy: "+ str(accu_test))

        sigmoid_random, softmax_random, sum_random = sess.run([max_output_sigmoid_noise, max_output_softmax_noise, noise_sum], {X_noisy: random_noise})
        writer.add_summary(sum_random, i)
        accu_random, sum_noisy = sess.run([accuracy_noisy, noisy_sum], {X_noisy: test_image_with_noise, Y_: mnist.test.labels})
        writer.add_summary(sum_noisy, i)
        print("Random noise images  - Sigmoid: " + str(sigmoid_random) + "\tSoftmax: " + str(softmax_random) + "\taccuracy: "+ str(accu_random))

        accu_adv, sigmoid_adv, softmax_adv, sum_adv = sess.run([accuracy_adv, max_output_sigmoid_adv, max_output_softmax_adv, adv_sum], {X_adv: adv_x_np, Y_: mnist.test.labels})
        writer.add_summary(sum_adv, i)
        print("Adversarial examples - Sigmoid: " + str(sigmoid_adv) + "\tSoftmax: " + str(softmax_adv) + "\taccuracy: "+ str(accu_adv))
        print()
        accuracy_list.append([i, accu_test, accu_random, accu_adv, counter])
        sigmoid_list.append([i, sigmoid_test, sigmoid_random, sigmoid_adv, counter])
        softmax_list.append([i, softmax_test, softmax_random, softmax_adv, counter])

    sess.run(c_train, {X: batch_X, Y_: batch_Y})
    if config_num == 1 or (config_num == 2 and i < 250000) or\
        config_num == 4 or (config_num == 5 and i < 250000):
        sess.run(d_train, {X: batch_X, Z: sample_Z(batch_size, Z_dim), BS: batch_size})
        sess.run(g_train, {Z: sample_Z(batch_size, Z_dim), BS: batch_size})

writer.close()

# Save data in csv
with open(folder_csv+"accuracy.csv", "w") as output:
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(accuracy_list)

with open(folder_csv+"sigmoid.csv", "w") as output:
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(sigmoid_list)

with open(folder_csv+"softmax.csv", "w") as output:
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(softmax_list)

# Load data in csv
accu_data = utils_csv.get_data_csv_file(folder_csv+"accuracy.csv")
sigmoid_data = utils_csv.get_data_csv_file(folder_csv+"sigmoid.csv")
softmax_data = utils_csv.get_data_csv_file(folder_csv+"softmax.csv")

# Print best values
utils_csv.print_best(accu_data, sigmoid_data, softmax_data, folder_csv+"summary.txt")
