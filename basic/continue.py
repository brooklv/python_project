#! /bin/python

while True:
	s = raw_input('Enter something:')
	if s == 'quit':
		break
	if len(s) < 3:
		continue;
	print "input is of sufficient length"

print 'Done!'
