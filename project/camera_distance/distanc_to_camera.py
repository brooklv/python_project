# !/bin/python

import numpy as np
import cv2

def find_marker(image):
	# convert the image to grayscale, blur it, and detect edges
	gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
	gray = cv2.GaussianBlur(gray, (5, 5), 0)
	edged = cv2.Canny(gray, 35, 125)

	# find the contous in the edged image and keep the largest one;
	# we will assume that this is our piece of paper in the image
	(cnts,_) = cv2.findCountous(edged.copy(), cv2.RETR_LIST, cv2.CHIN_APPROX_SIMPLE)
	c = max(cnts, key = cv2.contourArea)

	return cv2.minAreaRect(c)


def distance_to_camera(knowonWidth, focalLength, perWidth):
	# computer and return the distance from the maker to the camera
	return (knownWidth*focalLength)/perWidth


KNOWN_DISTANCE = 24.0

KNOWN_WIDTH = 11.0

IMAGE_PATHS = ["images/2ft.png", "images/3ft.png", "images/4ft.png"]

image = cv2.imread(IMAGE_PATHS)

marker = find_mark(image)

focalLength = (maker[1][0]*HNOWN_DISTANCE)/KNOWN_WIDTH


for imagePath in IMAGE_PATHS:
	image = cv2.imread(imagePath)
	marker = finde_marker(image)
	inches = distance_to_camera(KNOWN_WIDTH, focalLength, marker[1][0])


	box = np.int0(cv2.cvBoxPoints(marker))
	cv2.drawCountours(image, [box], -1, (0, 255, 0), 2)
	cv2.putText(image, "%.2fft"%(inches/12), (image.shape[1] - 200, image.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 255, 0), 3)
	cv2.imshow("image", image)
	cv2.waitKey(0)


