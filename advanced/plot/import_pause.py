
# -*- coding: utf-8 -*-
"""
Spyder Editor

This temporary script file is located here:
C:\Users\user\.spyder2\.temp.py
"""
"""
Show how to modify the coordinate formatter to report the image "z"
value of the nearest pixel given x and y
"""
# coding: utf-8

import time
import string
import os  
import math  
import pylab

import numpy as np
from numpy import genfromtxt
import matplotlib
import matplotlib as mpl
from matplotlib.colors import LogNorm
from matplotlib.mlab import bivariate_normal

import matplotlib.pyplot as plt
import matplotlib.cm as cm


import matplotlib.animation as animation

      
pause  = False
linenum=0

metric = genfromtxt('D:\export.csv', delimiter=',')

lines=len(metric)  
#print len(metric)
#print len(metric[4])
#print metric[4] 

rowdatas=metric[:,0]
for index in range(len(metric[4])-1):
    a=metric[:,index+1]
    rowdatas=np.row_stack((rowdatas,a))
    
#print len(rowdatas)
#print len(rowdatas[4])
#print rowdatas[4] 
#    

#plt.figure(figsize=(38,38), dpi=80)
#plt.plot(rowdatas[4] )
#plt.xlabel('time')
#plt.ylabel('value')
#plt.title("USBHID data analysis")
#plt.show()


##如果是参数是list,则默认每次取list中的一个元素,即metric[0],metric[1],... 
listdata=rowdatas.tolist()
print listdata[4]
#fig = plt.figure()  
#window = fig.add_subplot(111)  
#line, = window.plot(listdata[4] )  
 
#plt.ion()
#fig, ax = plt.subplots()
#line, = ax.plot(listdata[4],lw=2)
#ax.grid()


fig = plt.figure()  
ax = fig.add_subplot(111)  
line, = ax.plot(listdata[4],lw=2 ) # I'm still not clear on this stucture...
ax.grid()

time_template = 'Data ROW = %d'
time_text = ax.text(0.05, 0.9, '', transform=ax.transAxes)
 
#ax = plt.axes(xlim=(0, 700), ylim=(0, 255)) 
#line, = ax.plot([], [], lw=2) 
def onClick(event):
    global pause
    pause ^= True
    print 'user click the mouse!'
    print 'you pressed', event.button, event.xdata, event.ydata
#   event.button=1 鼠标左键按下 2 中键按下 3 右键按下    


def getData():  
    global listdata
    global linenum
    t = 0  
    while t < len(listdata[4]):
        if not pause: 
            linenum=linenum+1
        yield listdata[linenum-1]
#    while t < len(listdata[4]):  
#        t = t + 1  
#        print t,t
#        yield t, t  
        
def update(data):  
    global linenum
    line.set_ydata(data)    
    time_text.set_text(time_template % (linenum))
    return line,  

def init():
#    ax.set_ylim(0, 1.1)
#    ax.set_xlim(0, 10)
#    line.set_data(xdata)
    plt.xlabel('time')
    plt.ylabel('Time')
    plt.title('USBHID Data analysis')
    return line,
fig.canvas.mpl_connect('button_press_event', onClick)    
ani = animation.FuncAnimation(fig, update , getData , blit=False, interval=1*1000,init_func=init,repeat=False)  
plt.show()  


#my_data = genfromtxt('D:\export.csv', delimiter=',')
#rgbdata=my_data、255
#plt.figure(figsize=(38,38), dpi=80)
#
#for index in range(3):
#    row9=rgbdata[:,index]
#    print "row %d size is\n"%(index)
#    plt.plot(row9 )
#    plt.xlabel('time')
#    plt.ylabel('value')
#    plt.title("USBHID data analysis")
#    plt.legend()
##    plt.cla()
##    plt.clf()
#plt.show()
#plt.figure(1)
#plt.imshow(rgbdata, interpolation='nearest')
#plt.grid(True)
    
#fig = plt.figure() # 新图 0
#plt.savefig() # 保存
#plt.close('all') # 关闭图 0




