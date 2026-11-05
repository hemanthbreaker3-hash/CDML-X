from flask import Flask
app = Flask(__name__)

@app.route('/')
def hello_world():
    return 'GreyMatters'


if name == "__main__":
    app.run()
