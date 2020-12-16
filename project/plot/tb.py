# !/bin/python


import numpy as np
import pylab as pl

x = []
y = []
print "-----------------"
data = np.loadtxt("temp.txt", dtype=float) 
#f=open("timeBattery1.txt", "r").readlines()
#f = open("timeBattery1.txt", 'r')

#for item in f.readline():
#	x.append(item[0])
#	print x
print "----------------"
#w=f[0].split()
#print w[0]
#print w[1]
pl.plot(data[:,0], data[:,1], '.')
#pl.xlable('x')
#pl.ylable('y')
pl.show()

