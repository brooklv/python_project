#! /bin/python

def powersum(power, *args):
	'''Return the sum of each arguments raised to specified power.'''
	total = 0
	for i in args:
		total += pow(i, power)
	return total

print powersum(2,3,4)

print powersum(2,10)
