apiVersion: batch/v1
kind: CronJob
metadata:
  name: bywaymap-update
  namespace: office
spec:
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: trigger-update
              image: curlimages/curl:8.10.1
              command:
                - /bin/sh
                - -c
                - curl -fsS -X POST http://bywaymap.office.svc.cluster.local/update
