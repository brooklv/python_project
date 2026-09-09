#! /bin/python


def func():
	global x
	print 'x is', x
	x = 2
	print 'changed x to', x
x = 50
func()
print 'x is ', x

