#! /bin/python

import os
import time

source = ['/Users/lv/Documents/workspace/python', '/Users/lv/Documents/workspace/shell']

# back up dir
target_dir = '/Users/lv/Documents/workspace/backup/'

# set target format
target = target_dir + time.strftime('%y%m%d%H%M%S') + '.zip'

# set zip cmd
zip_command = "zip -qr '%s'  %s"%(target, ' '.join(source))

#print zip_command
# run the backup
if os.system(zip_command) == 0:
	print 'successful back up to', target
else:
	print 'Backup filed'

