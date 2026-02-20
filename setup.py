from setuptools import setup, find_packages

setup(
    name="camera-transfer",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "xxhash>=3.4.0",
        "rich>=13.7.0",
        "click>=8.1.0",
        "watchdog>=4.0.0",
        "pyobjc-framework-Speech>=10.0",
        "pyobjc-framework-AVFoundation>=10.0",
        "pyobjc-core>=10.0",
    ],
    entry_points={
        "console_scripts": [
            "camera-transfer=camera_transfer.main:cli",
        ],
    },
    python_requires=">=3.10",
)
