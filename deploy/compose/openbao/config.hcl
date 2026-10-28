# Local development OpenBao: real storage (survives restarts), plain HTTP on the private network.
storage "file" {
  path = "/openbao/file"
}
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
api_addr      = "http://openbao:8200"
ui            = true
