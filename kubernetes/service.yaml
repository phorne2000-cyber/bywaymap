apiVersion: v1
kind: Service
metadata:
  name: bywaymap
  namespace: office
spec:
  type: NodePort
  selector:
    app: bywaymap
  ports:
    - name: http
      port: 80
      targetPort: http
      nodePort: 30088
