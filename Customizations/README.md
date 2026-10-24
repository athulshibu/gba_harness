```
git clone https://github.com/athulshibu/skyemu_harness.git
cd skyemu_harness

git config --global user.email "athulshibu@gmail.com"
git config --global user.name "athul"

copy Customizations\.inputrc %LOCALAPPDATA%\clink
copy Customizations\config.yaml %APPDATA%\tabby

clink set autosuggest.strategy completion
clink set autosuggest.enable true
clink set autosuggest.hint false

```