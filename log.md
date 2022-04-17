## init & install

- pip install virtualenv
- virtualenv env
- env\Scripts\activate
- 解压之前下载的 superset 源码，进入到源码目录。
- (有代理不需要这步)pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
- pip install -e .
- pip install apache-superset
- pip install requests
- Pip install pillow
- pip install Werkzeug==2.0.0
- pip install jinja2==3.0.3
- superset db upgrade 初始化数据库
- superset fab create-admin
- superset load_examples
- superset init
- superset run -p 3000 --with-threads --reload --debugger
- cd superset-frontend
-  npm cache clear --force  
- npm install 
- 启动前端： npm run dev     run之后，刷新页面，热更新

## 容器部署SS

- npm run build 打包前端
