#! /bin/python

poem = '''\
Programing is fun
When the work is done
if you wanna make your work also fun:
	usr Python!
'''

f = file('poem.txt', 'w') #open for writing
f.write(poem) # write text to file
f.close() #close the file

f = file('poem.txt')
while True:
	line = f.readline()
	if len(line) == 0:
		break
	print line,
f.close()

