(function ($) {
    var $container = $('.portfolio');

    // Function to calculate column width and update item sizes
    function updatePortfolioLayout() {
        var w = $container.width();
        var columnNum = 1;
        var columnWidth = 50;

        if (w > 1200) {
            columnNum = 5;
        } else if (w > 900) {
            columnNum = 3;
        } else if (w > 600) {
            columnNum = 2;
        } else if (w > 300) {
            columnNum = 1;
        }

        columnWidth = Math.floor(w / columnNum);

        $container.find('.pitem').each(function () {
            var $item = $(this);
            var multiplier_w = $item.attr('class').match(/item-w(\d)/);
            var multiplier_h = $item.attr('class').match(/item-h(\d)/);

            var width = multiplier_w ? columnWidth * multiplier_w[1] - 0 : columnWidth - 5;
            var height = multiplier_h ? columnWidth * multiplier_h[1] * 1 - 5 : columnWidth * 0.5 - 5;

            $item.css({
                width: width,
                height: height
            });
        });

        // Re-layout Isotope after updating item sizes
        $container.isotope({
            masonry: {
                columnWidth: columnWidth,
                gutter: 10 // Adjust gutter as needed
            }
        }).isotope('layout');
    }


    // Filter items on button click
    $('nav.portfolio-filter ul a').on('click', function () {
        var selector = $(this).attr('data-filter');
        $container.isotope({ filter: selector });
        $('nav.portfolio-filter ul a').removeClass('active');
        $(this).addClass('active');
        return false;
    });

    // Initialize Isotope after images are loaded
    $container.imagesLoaded(function () {
        $container.isotope({
            itemSelector: '.pitem',
            layoutMode: 'masonry',
            masonry: {
                columnWidth: function( containerWidth ) {
                  return containerWidth / 5;
                },
                gutter: 10 // Adjust gutter as needed
            }
        });
    });

    // Update layout on window resize using a debounced function
    $(window).on('resize', $.debounce(250, function () {
        updatePortfolioLayout();
    }));

    // Initial layout update
    updatePortfolioLayout();

}(jQuery));


// Debounce function (simplest version)
(function($) {
  $.debounce = function(delay, fn) {
    var timer = null;
    return function () {
      var context = this, args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(context, args);
      }, delay);
    };
  };
})(jQuery);
