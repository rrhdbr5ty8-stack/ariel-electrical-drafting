#!/bin/sh
# Publish only the blank homepage, robots.txt, and social media files.
set -eu
rm -rf dist
mkdir -p dist
cp index.html robots.txt dist/
cp -a social dist/social
