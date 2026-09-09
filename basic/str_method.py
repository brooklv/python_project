#! /bin/python

name = "Swaroop" #this is a string object

if name.startswith('Swa'):
	print 'Yes, the string start with "Swa"'
if 'a' in name:
	print 'Yes, it contains the string "a"'

if name.find('war') != -1:
	print 'Yes, it contains the string "war"'

delimiter = '_*_'
mylist = ['Brazil', 'Russia', 'india', 'China']
print delimiter.join(mylist)

