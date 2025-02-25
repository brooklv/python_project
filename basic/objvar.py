#! /bin/python

class Person:
	''' Requests a person'''
	population = 0
	def __init__(self, name):
		'''Initializes the person`s data'''
		self.name = name
		print '(initializing %s)'%self.name
		
		# Add the population
		Person.population += 1
	
	def __del__(self):
		'''I am dying'''
		print '%s say bye.'%self.name
		Person.population -= 1
		if Person.population == 0:
			print 'I am the last one.'
		else:
			print 'There are still %d people left.'%Person.population

	def sayHi(self):
		'''Greeting by the person
		
		Really, That`s all it does'''

		print 'Hi, my name is %s'%self.name

	def howMany(self):
		'''Prints the current population.'''
		if Person.population == 1:
			print 'I am the only person here.'
		else:
			print 'We have %d people here.'%Person.population

swaroop = Person('Swaroop')
swaroop.sayHi()
swaroop.howMany()

kalcm = Person('Abdul Kalcm')
kalcm.sayHi()
kalcm.howMany()
swaroop.sayHi()
swaroop.howMany()

