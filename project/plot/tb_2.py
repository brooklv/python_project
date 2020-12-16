# !/bin/python


import numpy as np
import pylab as pl

x = []
y = []
print "-----------------"
data1 = np.loadtxt("battery1.txt", dtype=float) 
data2 = np.loadtxt("battery2.txt", dtype=float) 
#f=open("timeBattery1.txt", "r").readlines()
#f = open("timeBattery1.txt", 'r')

#for item in f.readline():
#	x.append(item[0])
#	print x
print "----------------"
#w=f[0].split()
#print w[0]
#print w[1]
pl.figure(1)
pl.plot(data1[:,1], data1[:,0], '.')
pl.plot(np.full(len(data1[:,1]), 600), '.')
#pl.xlable('x')
pl.figure(2)
pl.plot(data2[:,1], data2[:,0], '.')
pl.plot(np.full(len(data2[:,1]), 600), '.')
#pl.ylable('y')
pl.show()

