import setuptools
import os, stat

with open('README.md', 'r') as fh:
    long_description = fh.read()

setuptools.setup(
    name='hiraid',
    version='2.0.0',
    description='Hitachi raidcom wrapper',
    long_description=long_description,
    long_description_content_type='text/markdown',
    # author='Darren Chambers',
    # author_email='darren.chambers@hitachivantara.com',
    url='https://github.com/hitachi-vantara/hiraid-mainframe',
    packages=setuptools.find_packages(),
    install_requires=[
        'hicciexceptions'
    ],
    entry_points = { 'console_scripts':['historutil = hiraid.historutils.historutils:main'] }
)
