#! /bin/python

def printMax(x, y):
	'''print the maxmum of two numbers.
	
	the two value must be interger'''
	x = int(x)
	y = int(y)
	if x > y:
		print x,'is maxmum'
	else:
		print y,'is maxmum'
	
printMax(3, 5)
print printMax.__doc__
