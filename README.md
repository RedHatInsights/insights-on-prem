# Insights on Prem PoC

This repository contains all files necessary for running Insights on Prem. In the PoC phase, we are considering two different variants:

1. Deployment of all required components of the original pipeline 
   as it is currently runnning on console.redhat.com. Deployment files
   and guide to run the pipeline is located under `microservices` directory.
2. Development of separate new application that would provide 
   minimal features of the original pipeline and deployment
   of this app instead. This approach can be found under `monolithic` directory.