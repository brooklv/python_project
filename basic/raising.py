#! /bin/python

class ShortInputException(Exception):
	'''A usr-defined exception class.'''
	def __init__(self, length, atleast):
		Exception.__init__(self)
		self.length = length
		self.atleast = atleast

try:
	s = raw_input('Enter something-->')
	if len(s) < 3:
		raise ShortInputException(len(s), 3)

except EOFError:
	print '\nWhy did your do an EOF on me?'

except ShortInputException,x:
	print 'ShortInputException: The input was of length %d,\
	was expecting at least %d'%(x.length, x.atleast)

else:
	print 'No exception was raised.'
