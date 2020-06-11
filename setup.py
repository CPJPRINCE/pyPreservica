import pathlib
from setuptools import setup

# The directory containing this file
HERE = pathlib.Path(__file__).parent

# The text of the README file
README = (HERE / "README.md").read_text()

# This call to setup() does all the work
setup(
    name="pyPreservica",
    version="0.1.0",
    description="Python library for the Preservica Rest API",
    long_description=README.md,
    url="https://github.com/carj/pyPreservica",
    author="James Carr",
    author_email="james.carr@preservica.com",
    license="Apache License 2.0",
    packages=["pyPreservica"],
    install_requires=["requests", "uuid"]
)
