#! /bin/python

number = 23
guess = int(raw_input('Enter an interger:'))

if guess == number:
	print "congratulation, you guess it"
elif guess > number:
	print "input bigger !"
else:
	print "input samll !"

print "Done!"

