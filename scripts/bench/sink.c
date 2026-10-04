// sink port: answers every POST with 204 and closes. Counts requests; prints the count on SIGTERM.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <unistd.h>
typedef struct { int fd; char buf[16384]; int n; } C;
static long count; static void bye(int s){ fprintf(stderr,"sink %ld\n",count); _exit(0);} 
int main(int argc,char**argv){ signal(SIGTERM,bye); signal(SIGINT,bye); int port=atoi(argv[1]); int l=socket(AF_INET,SOCK_STREAM,0); int one=1; setsockopt(l,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);
  struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(port)}; a.sin_addr.s_addr=htonl(INADDR_LOOPBACK); if(bind(l,(void*)&a,sizeof a)||listen(l,1024)){perror("bind");return 1;}
  fcntl(l,F_SETFL,O_NONBLOCK); int ep=epoll_create1(0); struct epoll_event e={.events=EPOLLIN,.data.ptr=NULL}; epoll_ctl(ep,EPOLL_CTL_ADD,l,&e);
  const char*resp="HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"; struct epoll_event evs[256];
  for(;;){ int k=epoll_wait(ep,evs,256,-1); for(int j=0;j<k;j++){ C*c=evs[j].data.ptr;
    if(!c){ for(;;){ int fd=accept(l,0,0); if(fd<0)break; fcntl(fd,F_SETFL,O_NONBLOCK); C*n=calloc(1,sizeof(C)); n->fd=fd; struct epoll_event ne={.events=EPOLLIN,.data.ptr=n}; epoll_ctl(ep,EPOLL_CTL_ADD,fd,&ne);} continue; }
    int r=read(c->fd,c->buf+c->n,sizeof(c->buf)-c->n-1); if(r<=0){ if(r<0&&errno==EAGAIN)continue; close(c->fd); free(c); continue; } c->n+=r; c->buf[c->n]=0;
    char*h=strstr(c->buf,"\r\n\r\n"); if(!h)continue; int cl=0; char*p=strcasestr(c->buf,"content-length:"); if(p)cl=atoi(p+15); if(c->n<(h-c->buf)+4+cl)continue;
    if(write(c->fd,resp,strlen(resp))<0){} count++; close(c->fd); free(c); } } }
