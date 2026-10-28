library(shiny)
library(DBI)
library(fdp)
source_name <- Sys.getenv('POKEMON_SOURCE')
ui <- fluidPage(
  titlePanel(paste('Pokémon Collection ·', toupper(Sys.getenv('POKEMON_ENV')))),
  p('English TCGdex cards · governed, read-only managed identity · refreshes every minute'),
  sidebarLayout(sidebarPanel(textInput('search','Find a card',''),uiOutput('sets'),actionButton('refresh','Refresh data')),
    mainPanel(tabsetPanel(tabPanel('Collection',h3(textOutput('count')),tableOutput('summary'),plotOutput('types')),
      tabPanel('Cards',tableOutput('cards')),tabPanel('SCD2 history',tableOutput('history'))))),
  tags$small('Source: TCGdex. Pokémon and card artwork belong to their respective rights holders.'))
server <- function(input,output,session) {
  data <- reactive({
    invalidateLater(60000, session); input$refresh
    con <- fdp::connect(source_name);on.exit(dbDisconnect(con))
    list(cards=dbGetQuery(con,'SELECT * FROM analytics.cards_current ORDER BY set_name,card_name'),
         summary=dbGetQuery(con,'SELECT * FROM analytics.set_summary ORDER BY set_name'),
         history=dbGetQuery(con,'SELECT card_id,card_name,valid_from,valid_to,is_current FROM analytics.dim_cards_history ORDER BY valid_from DESC,card_id LIMIT 250'))
  })
  output$sets <- renderUI(selectInput('set','Set',c('All',sort(unique(data()$cards$set_name)))))
  filtered <- reactive({
    d<-data()$cards
    if(!is.null(input$set) && input$set!='All') d<-d[d$set_name==input$set,,drop=FALSE]
    if(nzchar(input$search)) d<-d[grepl(input$search,d$card_name,ignore.case=TRUE,fixed=TRUE),,drop=FALSE]
    d
  })
  output$count<-renderText(paste(nrow(filtered()),'cards in selection'))
  output$summary<-renderTable(data()$summary)
  output$cards<-renderTable(head(filtered()[,c('card_name','set_name','rarity','primary_type','hit_points')],100))
  output$history<-renderTable(data()$history)
  output$types<-renderPlot({x<-table(filtered()$primary_type);if(length(x))barplot(x,col='#3366aa',las=2,ylab='Cards')})
}
shinyApp(ui,server)
