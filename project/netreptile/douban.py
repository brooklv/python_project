#! /bin/python

from urllib2 import urlopen
from bs4 import BeautifulSoup as bs

resp = urlopen('https://movie.douban.com/nowplaying/hangzhou/')
html_data = resp.read().decode('utf-8')

# print html_data
soup = bs(html_data, 'html_parser')


