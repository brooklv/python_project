#! /bin/python

import sys

try:
	s = raw_input('Enter something-->')

except EOFError:
	print '\n Why did you do an EOF on me?'
	sys.exit()
except :
	print '\n Some Error/Exception occurred.'

print 'Done!'


