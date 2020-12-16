#! /bin/python

number = 23
running = True

while running:
	guess = int(raw_input('Enter an integer:'))
	if guess == number:
		print "you guess it !"
		running = False
	elif guess > number:
		print "input bigger !"
	else:
		print "input small !"
else:
	print 'the while loop is over!'

print 'Done!'
