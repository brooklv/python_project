#! /bin/python

def printMax(a, b):
	if a > b:
		print a, " is maxium"
	elif a < b:
		print b, " is maxium"
	else:
		print a, "==", b

a = raw_input("Enter number a:")
b = raw_input("Enter number b:")
printMax(a, b) 
