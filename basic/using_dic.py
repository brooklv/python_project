#! /bin/python

do={'Swaroop':'Swaroopch@byteofpython.info',
'Larry':'larry@wall.org',
'Mctsumoto':'mctz@ruby-lang.org',
'Spammer':'spammer@hotmail.com'
}

print "Swaroop`s address is %s" %do['Swaroop']

# add a key/value pair
do['Guido']='guido@python.org'

# delete a key/value pair
del do['Spammer']

print '\nThere are %d contants in the address-book\n' %len(do)
for name,address in do.items():
	print 'Contact %s at %s' %(name, address)

if 'Guido' in do:
	print '\nGuido`s address is %s' %do['Guido']


