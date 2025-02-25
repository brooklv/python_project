#! /bin/python

import os
import time

source = ['/Users/lv/Documents/workspace/python', '/Users/lv/Documents/workspace/shell']

target_dir = '/Users/lv/Documents/workspace/backup/'

today = target_dir + time.strftime('%Y%m%d')

now = time.strftime('%H%M%S')

comment = raw_input("Enter comment -->")
if len(comment) == 0:
	target = today + os.sep + now + '.zip'
else:
	target = today + os.sep + now + '_' + comment.replace(' ','_') + '.zip'

if not os.path.exists(today):
	os.mkdir(today)
	print 'Successfully created directory', today

zip_command = "zip -qr '%s' %s" %(target, ' '.join(source))

if os.system(zip_command) == 0:
	print 'Successfully backup to', target
else:
	print 'Backup FAILED'
