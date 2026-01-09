set -eu

if [ $1 == "main" ];then
    echo "python main.py"
    python main.py
else
    echo "$@"
    exec "$@"
fi

