# node
FROM node:20

# zip
RUN apt-get update && apt-get install -y zip

# aws cli
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && unzip awscliv2.zip
RUN ./aws/install && aws --version

# typescript
RUN npm install -g typescript@latest ts-node@latest

# aws cdk
RUN npm install -g aws-cdk@latest
