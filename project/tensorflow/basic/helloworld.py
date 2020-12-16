#! /bin/python

#source ~/tensorflow/bin/activate

import tensorflow as tf

# define the graph

helloap = tf.constant('hello world')

a = tf.constant(10)
b = tf.constant(32)

compute_op = tf.add(a, b)

# define the session to run graph
with tf.Session() as sess:
	print sess.run(helloap)
	print sess.run(compute_op)
