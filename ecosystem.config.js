module.exports = {
  apps: [{
    name: "pogojar",
    cwd: "/var/www/pogojar.com",
    script: "node_modules/next/dist/bin/next",
    args: "start -p 3002",
    instances: 1,
    env: {
      NODE_ENV: "production",
      PORT: 3002,
    },
  }],
}
