#! /bin/python


def func(a, b = 5, c = 10):
	print 'a is', a,'and b is',b,'and c is',c
func(3, 7)
func(25, c = 24)
func(c = 23, a = 100)

