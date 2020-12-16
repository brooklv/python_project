#! /bin/python

import os
import time

source = ['/Users/lv/Documents/workspace/shell', '/Users/lv/Documents/workspace/python']

target_dir = '/Users/lv/Documents/workspace/backup/'

today = target_dir + time.strftime('%Y%m%d')

now = time.strftime('%H%M%S')

if not os.path.exists(today):
	os.mkdir(today)
	print 'successfully created directory', today
# os.seq = /
target = today + os.sep + now + '.zip'

zip_command = "zip -qr '%s' %s"%(target, ' '.join(source))

#print zip_command

if os.system(zip_command) == 0:
	print 'Successful backup to', target
else:
	print 'Backup FAILED'

